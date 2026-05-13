"""数据库连接配置。

本项目不提供 SQLite 降级模式。开发、测试、演示和生产都应连接
PostgreSQL + TimescaleDB，确保 hypertable、唯一约束和压缩策略行为一致。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://finance_agent:finance_agent@localhost:5432/finance_agent"
)


def get_database_url() -> str:
    """读取数据库连接地址。"""

    return os.getenv("FINANCE_AGENT_DATABASE_URL", DEFAULT_DATABASE_URL)


def create_engine_from_settings(database_url: str | None = None) -> Engine:
    """创建 SQLAlchemy Engine。"""

    url = database_url or get_database_url()
    return create_engine(url, pool_pre_ping=True, future=True)


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    """创建数据库会话工厂。"""

    engine = create_engine_from_settings(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """提供事务边界，由调用方在服务层使用。"""

    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
