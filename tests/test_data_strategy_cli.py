from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.cli import data_sync


class _StrategyRepository:
    seeded: list[dict[str, Any]] = []
    records: dict[str, SimpleNamespace] = {}

    def __init__(self, session: object) -> None:
        self.session = session

    def seed_defaults(self, strategies: list[dict[str, Any]]) -> list[SimpleNamespace]:
        self.__class__.seeded = strategies
        self.__class__.records = {
            item["strategy_id"]: SimpleNamespace(**item) for item in strategies
        }
        return list(self.__class__.records.values())

    def list_strategies(
        self,
        *,
        market: str | None = None,
        status: str | None = None,
    ) -> list[SimpleNamespace]:
        records = list(self.__class__.records.values())
        if market:
            records = [item for item in records if item.market == market]
        if status:
            records = [item for item in records if item.status == status]
        return records

    def get_strategy(self, strategy_id: str) -> SimpleNamespace | None:
        return self.__class__.records.get(strategy_id)

    def upsert_strategy(self, **kwargs: Any) -> SimpleNamespace:
        record = SimpleNamespace(**kwargs)
        self.__class__.records[kwargs["strategy_id"]] = record
        return record


@contextmanager
def _session_scope(factory: object):
    yield object()


def _args(**kwargs: Any) -> argparse.Namespace:
    defaults = {
        "command": "strategy",
        "subcommand": "list",
        "database_url": "postgresql://test",
        "market": None,
        "status": None,
        "strategy_id": None,
        "name": None,
        "description": None,
        "group_weights": None,
        "missing_penalty": None,
        "activate": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture(autouse=True)
def _patch_strategy_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    _StrategyRepository.seeded = []
    _StrategyRepository.records = {}
    monkeypatch.setattr(data_sync, "ScoringStrategyRepository", _StrategyRepository)
    monkeypatch.setattr(data_sync, "create_session_factory", lambda database_url: object())
    monkeypatch.setattr(data_sync, "session_scope", _session_scope)


def test_data_strategy_init_seeds_defaults() -> None:
    """data strategy init 应幂等写入默认策略。"""

    result = data_sync.dispatch_data(_args(subcommand="init"))

    assert result["status"] == "ok"
    assert result["data"]["seeded_count"] >= 4
    assert "strategy:ashare:short_swing" in result["data"]["strategy_ids"]


def test_data_strategy_list_and_show_return_serializable_payloads() -> None:
    """list/show 应返回可序列化策略内容。"""

    data_sync.dispatch_data(_args(subcommand="init"))

    listed = data_sync.dispatch_data(_args(subcommand="list", market="ashare", status="active"))
    shown = data_sync.dispatch_data(
        _args(subcommand="show", strategy_id="strategy:ashare:short_swing")
    )

    assert listed["data"]["count"] == 4
    assert shown["data"]["strategy"]["strategy_id"] == "strategy:ashare:short_swing"
    assert shown["data"]["strategy"]["status"] == "active"


def test_data_strategy_set_validates_weight_sum() -> None:
    """set 激活策略前应校验权重和，避免产生不可解释评分。"""

    with pytest.raises(ValueError, match="权重和"):
        data_sync.dispatch_data(
            _args(
                subcommand="set",
                strategy_id="strategy:ashare:broken",
                market="ashare",
                name="错误策略",
                description="权重错误。",
                group_weights='{"technical":0.8,"fundamental":0.8}',
                missing_penalty='{"per_missing_group":4,"per_partial_group":1.5}',
                activate=True,
            )
        )


def test_data_strategy_set_upserts_draft_or_active_strategy() -> None:
    """set 应支持写入 draft，并可按参数激活。"""

    result = data_sync.dispatch_data(
        _args(
            subcommand="set",
            strategy_id="strategy:ashare:custom",
            market="ashare",
            name="自定义策略",
            description="测试策略。",
            group_weights='{"technical":0.7,"fundamental":0.3}',
            missing_penalty='{"per_missing_group":3,"per_partial_group":1}',
            activate=True,
        )
    )

    assert result["status"] == "ok"
    assert result["data"]["strategy"]["status"] == "active"
    assert result["data"]["strategy"]["group_weights"] == {
        "technical": 0.7,
        "fundamental": 0.3,
    }


def test_data_strategy_set_refuses_to_overwrite_active_strategy() -> None:
    """set 只能修改 draft 策略，避免误覆盖生产策略。"""

    data_sync.dispatch_data(_args(subcommand="init"))

    with pytest.raises(ValueError, match="只能修改 draft"):
        data_sync.dispatch_data(
            _args(
                subcommand="set",
                strategy_id="strategy:ashare:short_swing",
                market="ashare",
                name="误改短线策略",
                description="不应允许。",
                group_weights='{"technical":0.7,"fundamental":0.3}',
                missing_penalty='{"per_missing_group":3,"per_partial_group":1}',
            )
        )
