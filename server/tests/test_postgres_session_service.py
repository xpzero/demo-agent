import os
import unittest
from urllib.parse import urlparse

import psycopg
from dotenv import load_dotenv

from schema import SchemaError, apply_migrations, assert_connection_schema_current
from sessions import PostgresSessionService, Session, SessionRevisionConflict
from sessions.migrate_json import import_sessions


load_dotenv()
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "未配置 TEST_DATABASE_URL")
class PostgresSessionServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_name = urlparse(TEST_DATABASE_URL).path.removeprefix("/")
        if not database_name.endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL 的数据库名必须以 _test 结尾")
        if TEST_DATABASE_URL == os.getenv("DATABASE_URL"):
            raise RuntimeError("TEST_DATABASE_URL 必须与 DATABASE_URL 不同")
        apply_migrations(TEST_DATABASE_URL)

    def setUp(self):
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            connection.execute("TRUNCATE public.sessions RESTART IDENTITY")
        self.service = PostgresSessionService(TEST_DATABASE_URL)

    def tearDown(self):
        self.service.close()

    def test_crud_and_revision_conflict(self):
        created = self.service.create("system")
        current = self.service.get(created.id)
        stale = self.service.get(created.id)

        current.items.append({"role": "user", "content": "hello"})
        self.service.save(current)

        self.assertEqual(current.revision, 1)
        self.assertEqual(self.service.get(created.id).items[-1]["content"], "hello")
        with self.assertRaises(SessionRevisionConflict):
            self.service.save(stale)
        self.assertTrue(self.service.delete(created.id, current.revision))
        self.assertIsNone(self.service.get(created.id))

    def test_pending_approval_and_encrypted_reasoning_round_trip(self):
        session = self.service.create("system")
        session.items.append(
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
        )
        session.items.append(
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "write_file",
                "arguments": '{"path":"notes/demo.txt","content":"new"}',
            }
        )
        session.pending_approval = {
            "schema_version": 2,
            "remaining_turns": 9,
            "outputs_committed": False,
            "calls": [
                {
                    "id": "call_1",
                    "name": "write_file",
                    "args": {"path": "notes/demo.txt", "content": "new"},
                    "permission": {
                        "action": "ask",
                        "requests": [
                            {"permission": "write", "target": "notes/demo.txt"}
                        ],
                        "reason": "test",
                    },
                    "decision": None,
                    "outcome": None,
                    "output": None,
                    "preview": None,
                    "guard": None,
                }
            ],
        }

        self.service.save(session)
        restored = self.service.get(session.id)

        self.assertEqual(restored.items[-2]["encrypted_content"], "opaque")
        self.assertEqual(restored.pending_approval, session.pending_approval)

    def test_service_reopen_keeps_data(self):
        created = self.service.create("system")
        self.service.close()

        self.service = PostgresSessionService(TEST_DATABASE_URL)

        self.assertEqual(self.service.get(created.id).id, created.id)

    def test_migrations_are_idempotent(self):
        self.assertEqual(apply_migrations(TEST_DATABASE_URL), [])

    def test_json_import_preserves_id_and_advances_identity(self):
        imported = Session(
            id=7,
            items=[{"role": "system", "content": "system"}],
        )

        import_sessions(TEST_DATABASE_URL, [imported])

        self.assertEqual(self.service.get(7).id, 7)
        self.assertEqual(self.service.create("next").id, 8)

    def test_json_import_preserves_existing_identity_high_water_mark(self):
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            connection.execute(
                "SELECT setval('public.sessions_id_seq', 100, true)"
            )

        import_sessions(
            TEST_DATABASE_URL,
            [Session(id=7, items=[{"role": "system", "content": "system"}])],
        )

        self.assertEqual(self.service.create("next").id, 101)

    def test_schema_check_detects_missing_default(self):
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            connection.execute(
                "ALTER TABLE public.sessions ALTER COLUMN revision DROP DEFAULT"
            )
            with self.assertRaises(SchemaError):
                assert_connection_schema_current(connection)
            connection.rollback()


if __name__ == "__main__":
    unittest.main()
