"""盘中波动触发规则测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.monitoring.models import PositionAction
from finance_agent.triggers.service import TriggerEvaluationRequest, TriggerService


class _ScalarResult:
    """模拟 SQLAlchemy scalars 返回值。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """只返回测试指定的实时快照。"""

    def __init__(self, snapshots: list[Any]) -> None:
        self.snapshots = snapshots

    def scalars(self, _statement: Any) -> _ScalarResult:
        return _ScalarResult(self.snapshots)


class _FakeTriggerRepository:
    """记录触发事件 upsert 入参。"""

    def __init__(self) -> None:
        self.events: list[SimpleNamespace] = []

    def has_recent_event(self, *, dedup_key: str, since: datetime) -> bool:
        del dedup_key, since
        return False

    def upsert_trigger_event(self, **values: Any) -> SimpleNamespace:
        event = SimpleNamespace(**values)
        self.events.append(event)
        return event


class _FakeWatchlists:
    """返回测试指定的观察池项目。"""

    def __init__(self, items: tuple[Any, ...]) -> None:
        self.items = items

    def list_active_items(self, *, owner_id: str, watchlist_id: str | None = None) -> tuple[Any, ...]:
        del owner_id, watchlist_id
        return self.items


def _quote(
    *,
    asset_id: str = "ashare:600519",
    symbol: str = "600519",
    as_of: datetime,
    last_price: str,
    volume: str,
) -> SimpleNamespace:
    """构造实时行情快照。"""

    return SimpleNamespace(
        asset_id=asset_id,
        symbol=symbol,
        market="ashare",
        source="test",
        as_of=as_of,
        last_price=Decimal(last_price),
        volume=Decimal(volume),
        status="available",
    )


def _position_snapshot(*, asset_id: str = "ashare:600519", symbol: str = "600519") -> Any:
    """构造最小持仓快照。"""

    return SimpleNamespace(
        portfolio=SimpleNamespace(portfolio_id="portfolio:default-owner"),
        positions=(
            SimpleNamespace(
                position_id="position:600519",
                asset_id=asset_id,
                symbol=symbol,
            ),
        ),
    )


def _watchlist_item(*, asset_id: str = "ashare:300750", symbol: str = "300750") -> Any:
    """构造最小观察池项目。"""

    return SimpleNamespace(
        watchlist_item_id="watchlist-item:300750",
        watchlist_id="watchlist:default-owner:ashare:research",
        asset_id=asset_id,
        symbol=symbol,
    )


def _service(
    *,
    snapshots: list[Any],
    portfolio_snapshots: tuple[Any, ...] = (),
    watchlist_items: tuple[Any, ...] = (),
) -> TriggerService:
    """构造只启用盘中规则的触发服务。"""

    service = object.__new__(TriggerService)
    service.session = _FakeSession(snapshots)
    service.triggers = _FakeTriggerRepository()
    service.watchlists = _FakeWatchlists(watchlist_items)
    service._load_portfolio_snapshots = lambda _request: portfolio_snapshots
    service._evaluate_position_triggers = lambda **_kwargs: []
    service._evaluate_watchlist_triggers = lambda **_kwargs: []
    service._evaluate_recommendation_triggers = lambda **_kwargs: []
    service._evaluate_risk_triggers = lambda **_kwargs: []
    service._evaluate_data_quality_triggers = lambda **_kwargs: []
    return service


def _request(as_of: datetime) -> TriggerEvaluationRequest:
    """构造只评估盘中波动的请求。"""

    return TriggerEvaluationRequest(
        owner_id="default-owner",
        as_of=as_of,
        trigger_groups=("intraday_volatility",),
        intraday_sharp_drop_threshold=Decimal("-0.04"),
        intraday_volume_surge_multiplier=Decimal("3"),
        cooldown_minutes=120,
    )


def test_intraday_sharp_drop_triggers_portfolio_monitoring() -> None:
    """持仓标的最近两次快照跌幅超过阈值时，应触发持仓监控。"""

    as_of = datetime(2026, 6, 12, 10, 30, tzinfo=UTC)
    service = _service(
        snapshots=[
            _quote(as_of=as_of, last_price="96", volume="1200"),
            _quote(as_of=as_of - timedelta(minutes=5), last_price="100", volume="1000"),
        ],
        portfolio_snapshots=(_position_snapshot(),),
    )

    result = service.evaluate(_request(as_of))

    assert len(result.created_events) == 1
    event = result.created_events[0]
    assert event.trigger_type == "intraday_sharp_drop"
    assert event.requested_workflow_type == "portfolio_monitoring"
    assert event.portfolio_id == "portfolio:default-owner"
    assert event.payload["price_change_ratio"] == "-0.040000"


def test_intraday_volume_surge_triggers_asset_deep_analysis_for_watchlist() -> None:
    """观察池标的放量且价格变动超过阈值时，应触发单资产深度分析。"""

    as_of = datetime(2026, 6, 12, 10, 30, tzinfo=UTC)
    history = [
        _quote(
            asset_id="ashare:300750",
            symbol="300750",
            as_of=as_of - timedelta(minutes=minute),
            last_price="100",
            volume="100",
        )
        for minute in range(5, 105, 5)
    ]
    service = _service(
        snapshots=[
            _quote(
                asset_id="ashare:300750",
                symbol="300750",
                as_of=as_of,
                last_price="103",
                volume="350",
            ),
            *history,
        ],
        watchlist_items=(_watchlist_item(),),
    )

    result = service.evaluate(_request(as_of))

    assert len(result.created_events) == 1
    event = result.created_events[0]
    assert event.trigger_type == "intraday_volume_surge"
    assert event.requested_workflow_type == "asset_deep_analysis"
    assert event.watchlist_id == "watchlist:default-owner:ashare:research"
    assert event.payload["volume_surge_multiplier"] == "3.500000"


def test_intraday_volatility_skips_when_quote_data_is_insufficient() -> None:
    """实时快照不足时应静默跳过并计数，不应生成触发事件。"""

    as_of = datetime(2026, 6, 12, 10, 30, tzinfo=UTC)
    service = _service(
        snapshots=[_quote(as_of=as_of, last_price="100", volume="100")],
        portfolio_snapshots=(_position_snapshot(),),
    )

    result = service.evaluate(_request(as_of))

    assert result.created_events == ()
    assert result.skipped_no_data_count == 1


def test_persist_position_actions_keeps_unexecutable_intended_action() -> None:
    """监控动作写入触发事件时，不可执行状态仍须保留原计划动作。"""

    as_of = datetime(2026, 6, 12, 10, 30, tzinfo=UTC)
    service = _service(snapshots=[])
    action = PositionAction(
        position_id="position:600519",
        action="unexecutable",
        intended_action="exit",
        severity="high",
        reason_codes=("quote_missing",),
        evaluated_at=as_of,
        payload={
            "owner_id": "default-owner",
            "portfolio_id": "portfolio:default-owner",
            "asset_id": "ashare:600519",
            "symbol": "600519",
        },
    )

    result = service.persist_position_actions((action,), as_of=as_of)

    assert len(result.created_events) == 1
    event = result.created_events[0]
    assert event.trigger_type == "position_monitoring_action"
    assert event.requested_workflow_type == "portfolio_monitoring"
    assert event.payload["action"] == "unexecutable"
    assert event.payload["intended_action"] == "exit"
    assert event.payload["reason_codes"] == ["quote_missing"]
