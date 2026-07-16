"""数据库连接配置。

本项目不提供 SQLite 降级模式。开发、测试、演示和生产都应连接
PostgreSQL + TimescaleDB，确保 hypertable、唯一约束和压缩策略行为一致。
"""

from __future__ import annotations

import os
from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from threading import RLock

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://finance_agent:finance_agent@localhost:5432/finance_agent"
)
DB_POOL_SIZE_ENV = "FINANCE_AGENT_DB_POOL_SIZE"
DB_MAX_OVERFLOW_ENV = "FINANCE_AGENT_DB_MAX_OVERFLOW"
DB_POOL_TIMEOUT_SECONDS_ENV = "FINANCE_AGENT_DB_POOL_TIMEOUT_SECONDS"
DB_POOL_RECYCLE_SECONDS_ENV = "FINANCE_AGENT_DB_POOL_RECYCLE_SECONDS"
DEFAULT_DB_POOL_SIZE = 10
DEFAULT_DB_MAX_OVERFLOW = 20
DEFAULT_DB_POOL_TIMEOUT_SECONDS = 30
DEFAULT_DB_POOL_RECYCLE_SECONDS = 1800

_ENGINE_CACHE_LOCK = RLock()
_ENGINE_CACHE: dict[tuple[Hashable, ...], Engine] = {}
_SESSION_FACTORY_CACHE: dict[tuple[Hashable, ...], sessionmaker[Session]] = {}


def get_database_url() -> str:
    """读取数据库连接地址。"""

    return os.getenv("FINANCE_AGENT_DATABASE_URL", DEFAULT_DATABASE_URL)


def create_engine_from_settings(database_url: str | None = None) -> Engine:
    """创建 SQLAlchemy Engine。"""

    url = database_url or get_database_url()
    pool_options = _database_pool_options()
    cache_key = _engine_cache_key(url, pool_options)
    with _ENGINE_CACHE_LOCK:
        cached_engine = _ENGINE_CACHE.get(cache_key)
        if cached_engine is not None:
            return cached_engine
        engine = create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            **pool_options,
        )
        _ENGINE_CACHE[cache_key] = engine
        return engine


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    """创建数据库会话工厂。"""

    url = database_url or get_database_url()
    pool_options = _database_pool_options()
    cache_key = _engine_cache_key(url, pool_options)
    with _ENGINE_CACHE_LOCK:
        cached_factory = _SESSION_FACTORY_CACHE.get(cache_key)
        if cached_factory is not None:
            return cached_factory
        engine = create_engine_from_settings(url)
        factory = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        _SESSION_FACTORY_CACHE[cache_key] = factory
        return factory


def dispose_cached_engines() -> None:
    """释放当前进程内缓存的数据库 Engine，并清空会话工厂缓存。"""

    with _ENGINE_CACHE_LOCK:
        engines = list(_ENGINE_CACHE.values())
        _SESSION_FACTORY_CACHE.clear()
        _ENGINE_CACHE.clear()
    for engine in engines:
        engine.dispose()


def _database_pool_options() -> dict[str, int]:
    """读取数据库连接池配置，给长进程一个明确连接上限。"""

    return {
        "pool_size": _env_int(DB_POOL_SIZE_ENV, DEFAULT_DB_POOL_SIZE, minimum=1),
        "max_overflow": _env_int(DB_MAX_OVERFLOW_ENV, DEFAULT_DB_MAX_OVERFLOW, minimum=0),
        "pool_timeout": _env_int(
            DB_POOL_TIMEOUT_SECONDS_ENV,
            DEFAULT_DB_POOL_TIMEOUT_SECONDS,
            minimum=1,
        ),
        "pool_recycle": _env_int(
            DB_POOL_RECYCLE_SECONDS_ENV,
            DEFAULT_DB_POOL_RECYCLE_SECONDS,
            minimum=1,
        ),
    }


def _engine_cache_key(url: str, pool_options: dict[str, int]) -> tuple[Hashable, ...]:
    """构造 Engine/SessionFactory 缓存键。"""

    return (
        url,
        pool_options["pool_size"],
        pool_options["max_overflow"],
        pool_options["pool_timeout"],
        pool_options["pool_recycle"],
    )


def _env_int(name: str, default: int, *, minimum: int) -> int:
    """读取整数环境变量，非法值退回默认值。"""

    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(value, minimum)


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
