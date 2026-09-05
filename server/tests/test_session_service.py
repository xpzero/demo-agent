import unittest

from fakes import FakeSessionService
from sessions import Session, SessionDataError, SessionRevisionConflict
from sessions.postgres import _plain_session


class SessionServiceContractTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeSessionService()

    def test_create_persists_empty_session_and_list_is_sorted(self):
        first = self.service.create("system")
        second = self.service.create("system")

        self.assertEqual((first.id, second.id), (1, 2))
        self.assertEqual(self.service.get(first.id).to_dict(), first.to_dict())
        self.assertEqual(
            [summary.id for summary in self.service.list_sessions()], [1, 2]
        )
        self.assertEqual(self.service.list_sessions()[0].summary, "(空会话)")

    def test_save_increments_revision_and_rejects_stale_snapshot(self):
        created = self.service.create("system")
        first = self.service.get(created.id)
        stale = self.service.get(created.id)

        first.items.append({"role": "user", "content": "first"})
        self.service.save(first)

        self.assertEqual(first.revision, 1)
        stale.items.append({"role": "user", "content": "stale"})
        with self.assertRaises(SessionRevisionConflict):
            self.service.save(stale)

        restored = self.service.get(created.id)
        self.assertEqual(restored.revision, 1)
        self.assertEqual(restored.items[-1]["content"], "first")

    def test_delete_checks_revision(self):
        created = self.service.create("system")
        current = self.service.get(created.id)
        stale_revision = current.revision
        current.items.append({"role": "user", "content": "updated"})
        self.service.save(current)

        with self.assertRaises(SessionRevisionConflict):
            self.service.delete(created.id, stale_revision)

        self.assertTrue(self.service.delete(created.id, current.revision))
        self.assertIsNone(self.service.get(created.id))

    def test_postgres_payload_rejects_nul_character(self):
        session = Session(
            id=1,
            items=[{"role": "user", "content": "before\x00after"}],
        )

        with self.assertRaises(SessionDataError):
            _plain_session(session)

    def test_postgres_payload_accepts_literal_nul_escape_text(self):
        session = Session(
            id=1,
            items=[{"role": "user", "content": r"literal \u0000 text"}],
        )

        items, pending = _plain_session(session)

        self.assertEqual(items, session.items)
        self.assertIsNone(pending)


if __name__ == "__main__":
    unittest.main()
