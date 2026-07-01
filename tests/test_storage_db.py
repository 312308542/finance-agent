from __future__ import annotations

from typing import Any

from finance_agent.storage import db


class _FakeEngine:
    def __init__(self) -> None:
        self.dispose_count = 0

    def dispose(self) -> None:
        self.dispose_count += 1


def test_create_session_factory_reuses_engine_for_same_database_url(monkeypatch) -> None:
    """同一进程内同一数据库地址应复用 Engine，避免调度长进程反复堆 idle 连接。"""

    db.dispose_cached_engines()
    created_engines: list[_FakeEngine] = []

    def fake_create_engine(url: str, **kwargs: Any) -> _FakeEngine:
        created_engines.append(_FakeEngine())
        return created_engines[-1]

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    try:
        first = db.create_session_factory("postgresql+psycopg://user:pass@localhost:5432/app")
        second = db.create_session_factory("postgresql+psycopg://user:pass@localhost:5432/app")
    finally:
        db.dispose_cached_engines()

    assert first is second
    assert len(created_engines) == 1


def test_create_engine_from_settings_uses_bounded_pool_defaults(monkeypatch) -> None:
    """默认连接池必须有明确上限，避免每个 Engine 继承 SQLAlchemy 默认容量。"""

    db.dispose_cached_engines()
    calls: list[dict[str, Any]] = []

    def fake_create_engine(url: str, **kwargs: Any) -> _FakeEngine:
        calls.append({"url": url, "kwargs": kwargs})
        return _FakeEngine()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    try:
        db.create_engine_from_settings("postgresql+psycopg://user:pass@localhost:5432/app")
    finally:
        db.dispose_cached_engines()

    assert calls[0]["kwargs"] == {
        "pool_pre_ping": True,
        "future": True,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 30,
        "pool_recycle": 1800,
    }


def test_dispose_cached_engines_closes_and_clears_cache(monkeypatch) -> None:
    """显式清理缓存时应 dispose 旧 Engine，并允许后续重新创建。"""

    db.dispose_cached_engines()
    created_engines: list[_FakeEngine] = []

    def fake_create_engine(url: str, **kwargs: Any) -> _FakeEngine:
        created_engines.append(_FakeEngine())
        return created_engines[-1]

    monkeypatch.setattr(db, "create_engine", fake_create_engine)

    first = db.create_session_factory("postgresql+psycopg://user:pass@localhost:5432/app")
    db.dispose_cached_engines()
    second = db.create_session_factory("postgresql+psycopg://user:pass@localhost:5432/app")

    try:
        assert first is not second
        assert len(created_engines) == 2
        assert created_engines[0].dispose_count == 1
    finally:
        db.dispose_cached_engines()
