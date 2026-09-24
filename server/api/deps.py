"""API 层共享依赖：数据库入口、路径常量与会话运行锁。"""

import logging
from threading import Lock

from database import DATABASE_PATH as DEFAULT_DATABASE_PATH
from database import Database
from documents import FILE_ROOT as DEFAULT_FILE_ROOT
from logging_setup import setup_logging

setup_logging()

DATABASE_PATH = DEFAULT_DATABASE_PATH
FILE_ROOT = DEFAULT_FILE_ROOT

_running_sessions: set[str] = set()
_lock = Lock()


def get_database() -> Database:
    database = Database(DATABASE_PATH)
    database.initialize()
    return database


logger = logging.getLogger(__name__)
