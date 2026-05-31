"""FastAPI 依赖。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from finance_agent.storage.db import create_session_factory, session_scope


def get_session() -> Iterator[Session]:
    """为单次 HTTP 请求提供数据库会话。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        yield session
