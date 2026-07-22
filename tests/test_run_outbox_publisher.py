"""Outbox 发布器命令入口测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _load_module() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "events" / "run_outbox_publisher.py"
    spec = importlib.util.spec_from_file_location("run_outbox_publisher", path)
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载发布器脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_once_uses_database_outbox_and_redis_transport(monkeypatch) -> None:
    module = _load_module()
    calls: list[dict[str, Any]] = []

    class _Session:
        pass

    class _Context:
        def __enter__(self) -> _Session:
            return _Session()

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(module, "create_session_factory", lambda _url: "factory")
    monkeypatch.setattr(module, "session_scope", lambda _factory: _Context())

    class _Publisher:
        def __init__(self, repository: Any, transport: Any) -> None:
            calls.append({"repository": repository, "transport": transport})

        def publish_batch(self, **kwargs: Any) -> int:
            calls.append(kwargs)
            return 2

    monkeypatch.setattr(module, "OutboxPublisher", _Publisher)

    result = module.publish_once(redis_client=SimpleNamespace(), publisher_id="publisher-1")

    assert result == 2
    assert calls[1]["publisher_id"] == "publisher-1"
