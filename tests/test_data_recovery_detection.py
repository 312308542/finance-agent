"""H2/H3/H4 针对性单元测试：缺口检测与事实验证规则。

覆盖 codex 审查 HIGH 三项：
- H2 基本面目标必须按资产粒度生成并可验证；
- H3 估值检测从全体资产池出发，验证门槛用计划要求窗口起点；
- H4 扫描起点由水位推导（366 天下限），K 线检测应用资产生命周期。

全部使用 SimpleNamespace/假 Session 注入（参考 test_data_recovery_service.py），
不连接真实数据库。
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from finance_agent.data_recovery.gap_detector import (
    FUNDAMENTAL_REPORT_SOURCES,
    AssetLifecycleWindow,
    DomainGapQueries,
)
from finance_agent.data_recovery.service import DataRecoveryModule
from finance_agent.data_recovery.verifier import RecoveryVerifier

CUTOFF_AT = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)  # 上海时间 11-13 深夜
CUTOFF_DATE = date(2026, 11, 13)
REQUIRED_DAY = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)  # cutoff 前一自然日零点


class _FakeResult:
    """按预置行集返回 all()/scalar_one_or_none()。"""

    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None


class _FakeSession:
    """按队列顺序返回预置结果集，并记录执行过的语句。"""

    def __init__(self, results) -> None:
        self._results = list(results)
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult(self._results.pop(0))


# ------------------------------------------------------------------
# H2：基本面目标资产粒度 + 验证覆盖真实 asset_id
# ------------------------------------------------------------------


def test_fundamental_gap_targets_are_asset_scoped() -> None:
    session = _FakeSession(
        [
            [
                ("ashare:600000", "20260630"),  # 报告期落后
                ("ashare:000001", "20260930"),  # 已达标
            ]
        ]
    )
    targets = DomainGapQueries(session).detect_fundamental_gap_targets(
        asset_ids=["ashare:600000", "ashare:000001"], cutoff_at=CUTOFF_AT
    )
    assert [target.asset_id for target in targets] == ["ashare:600000"]
    assert all(target.asset_id is not None for target in targets)
    assert all(target.data_domain == "fundamentals" for target in targets)
    assert targets[0].exception_evidence["expected_report_period"] == "2026-09-30"


def test_verifier_fundamentals_binds_real_asset_id() -> None:
    session = _FakeSession([[("20260930",)]])
    verifier = RecoveryVerifier(session)
    row = SimpleNamespace(
        data_domain="fundamentals",
        asset_id="ashare:600000",
        gap_start_at=datetime(2026, 9, 30, tzinfo=UTC),
        gap_end_at=CUTOFF_AT,
        granularity="report",
        exception_evidence={"expected_report_period": "2026-09-30"},
    )
    status, code, evidence = verifier.verify_target(row)
    assert status == "completed"
    assert code is None
    # 验证语句按真实 asset_id 绑定参数，不再形成 IN (NULL)；
    # 且只认权威财务指标来源（复审 MEDIUM）
    compiled = session.statements[0].compile()
    assert list(compiled.params.values()) == [
        ["ashare:600000"],
        list(FUNDAMENTAL_REPORT_SOURCES),
    ]


def test_verifier_fundamentals_without_asset_scope_fails_transient() -> None:
    session = _FakeSession([])
    row = SimpleNamespace(
        data_domain="fundamentals",
        asset_id=None,
        gap_start_at=datetime(2026, 9, 30, tzinfo=UTC),
        gap_end_at=CUTOFF_AT,
        granularity="report",
        exception_evidence={},
    )
    status, _, evidence = RecoveryVerifier(session).verify_target(row)
    assert status == "failed_transient"
    assert evidence["error"] == "fundamental_target_without_asset_scope"
    assert session.statements == []  # 不再执行恒假查询


def test_verifier_fundamentals_recomputes_expected_period_when_evidence_missing() -> None:
    session = _FakeSession([[("20260831",)]])
    row = SimpleNamespace(
        data_domain="fundamentals",
        asset_id="ashare:600000",
        gap_start_at=datetime(2026, 9, 30, tzinfo=UTC),
        gap_end_at=CUTOFF_AT,
        granularity="report",
        exception_evidence={},
    )
    status, _, evidence = RecoveryVerifier(session).verify_target(row)
    assert status == "failed_transient"
    assert evidence["asset_id"] == "ashare:600000"
    assert evidence["expected_report_period"] == "2026-09-30"


# ------------------------------------------------------------------
# H3：估值检测覆盖无记录资产 + 验证门槛用计划要求日期
# ------------------------------------------------------------------


def test_valuation_gap_targets_include_assets_without_records() -> None:
    stale_day = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    fresh_day = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
    session = _FakeSession([[("ashare:000001", fresh_day), ("ashare:000002", stale_day)]])
    targets = DomainGapQueries(session).detect_valuation_gap_targets(
        asset_ids=["ashare:600000", "ashare:000001", "ashare:000002"],
        required_as_of=REQUIRED_DAY,
        cutoff_at=CUTOFF_AT,
    )
    by_asset = {target.asset_id: target for target in targets}
    # 从未写入估值快照的资产同样进入缺口集合
    assert set(by_asset) == {"ashare:600000", "ashare:000002"}
    missing_record = by_asset["ashare:600000"]
    assert missing_record.gap_start_at == REQUIRED_DAY
    assert missing_record.exception_evidence["required_as_of"] == REQUIRED_DAY.isoformat()
    # 仍有旧记录的资产目标从旧截面起补
    assert by_asset["ashare:000002"].gap_start_at == stale_day
    # 已达标资产不产生目标
    assert "ashare:000001" not in by_asset


class _SpyQueries:
    """记录 detect_valuation_gap_targets 收到的门槛参数。"""

    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    def detect_valuation_gap_targets(self, *, asset_ids, required_as_of, cutoff_at):
        self.calls.append(
            {
                "asset_ids": list(asset_ids),
                "required_as_of": required_as_of,
                "cutoff_at": cutoff_at,
            }
        )
        return self.result


def _valuation_row(**overrides):
    values = dict(
        data_domain="valuation",
        asset_id="ashare:000002",
        gap_start_at=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),  # 旧记录时间
        gap_end_at=CUTOFF_AT,
        granularity="snapshot",
        exception_evidence={"required_as_of": REQUIRED_DAY.isoformat()},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_valuation_verifier_threshold_ignores_stale_record_time() -> None:
    spy = _SpyQueries(result=[SimpleNamespace()])  # 模拟仍未补齐
    verifier = RecoveryVerifier(_FakeSession([]))
    verifier.queries = spy
    status, _, evidence = verifier.verify_target(_valuation_row())
    assert status == "failed_transient"
    assert spy.calls[0]["required_as_of"] == REQUIRED_DAY
    assert spy.calls[0]["required_as_of"] != _valuation_row().gap_start_at
    assert evidence["required_as_of"] == REQUIRED_DAY.isoformat()


def test_valuation_verifier_completes_against_plan_window() -> None:
    spy = _SpyQueries(result=[])  # 最新截面已达计划窗口起点 → 补齐
    verifier = RecoveryVerifier(_FakeSession([]))
    verifier.queries = spy
    status, _, _ = verifier.verify_target(_valuation_row())
    assert status == "completed"  # 尽管目标行携带的旧记录时间早于窗口起点


def test_valuation_verifier_falls_back_to_cutoff_window_without_evidence() -> None:
    spy = _SpyQueries(result=[])
    verifier = RecoveryVerifier(_FakeSession([]))
    verifier.queries = spy
    status, _, _ = verifier.verify_target(_valuation_row(exception_evidence={}))
    assert status == "completed"
    assert spy.calls[0]["required_as_of"] == REQUIRED_DAY


def test_valuation_verifier_requires_asset_scope() -> None:
    spy = _SpyQueries(result=[])
    verifier = RecoveryVerifier(_FakeSession([]))
    verifier.queries = spy
    status, _, evidence = verifier.verify_target(_valuation_row(asset_id=None))
    assert status == "failed_transient"
    assert evidence["error"] == "valuation_target_without_asset_scope"
    assert spy.calls == []


# ------------------------------------------------------------------
# H4：扫描起点水位推导 + K 线资产生命周期
# ------------------------------------------------------------------


def _module_with_watermarks(mapping):
    module = DataRecoveryModule(None)
    module.queries = SimpleNamespace(
        domain_watermark_earliest=lambda *, data_domain: mapping.get(data_domain)
    )
    return module


def _floor(cutoff_date):
    return cutoff_date - timedelta(days=366)


def test_scan_start_uses_earliest_domain_watermark() -> None:
    module = _module_with_watermarks(
        {
            "market_bars": datetime(2026, 5, 1, tzinfo=UTC),
            "events": datetime(2026, 7, 1, tzinfo=UTC),
        }
    )
    assert module._scan_start_date(cutoff_date=CUTOFF_DATE) == date(2026, 5, 1)


def test_scan_start_keeps_genuinely_old_watermark() -> None:
    # 复审 H4-①：真实老水位不得被固定窗口截断丢弃
    module = _module_with_watermarks({"market_bars": datetime(2020, 1, 1, tzinfo=UTC)})
    assert module._scan_start_date(cutoff_date=CUTOFF_DATE) == date(2020, 1, 1)


def test_scan_start_uses_min_within_domain_not_max() -> None:
    # 复审 H4-②：域内最早水位决定起点，单个新水位不掩盖旧资产水位
    module = _module_with_watermarks(
        {"market_bars": datetime(2025, 3, 1, tzinfo=UTC)}  # 远早于其他域
    )
    assert module._scan_start_date(cutoff_date=CUTOFF_DATE) == date(2025, 3, 1)


def test_scan_start_defaults_to_floor_without_watermarks() -> None:
    module = _module_with_watermarks({})
    assert module._scan_start_date(cutoff_date=CUTOFF_DATE) == _floor(CUTOFF_DATE)


def test_scan_start_treats_future_watermark_as_anomaly() -> None:
    module = _module_with_watermarks({"valuation": datetime(2027, 1, 1, tzinfo=UTC)})
    assert module._scan_start_date(cutoff_date=CUTOFF_DATE) == _floor(CUTOFF_DATE)


def test_fixed_sixty_day_lookback_removed() -> None:
    from finance_agent.data_recovery import service as recovery_service

    assert not hasattr(recovery_service, "SCAN_LOOKBACK_DAYS")
    assert not hasattr(recovery_service, "MIN_SCAN_LOOKBACK_DAYS")
    assert recovery_service.FALLBACK_SCAN_LOOKBACK_DAYS >= 366


TRADING_DAYS = [date(2026, 8, d) for d in (3, 4, 5, 6, 7, 10, 11)]


def test_asset_lifecycle_window_filters_invalid_dates() -> None:
    window = AssetLifecycleWindow(
        asset_id="ashare:x",
        list_date=date(2026, 8, 5),
        delist_date=date(2026, 8, 10),
        suspended_dates=(date(2026, 8, 7),),
    )
    # 退市日当天仍需覆盖，退市日之后剔除
    assert window.required_dates(TRADING_DAYS) == [
        date(2026, 8, 5),
        date(2026, 8, 6),
        date(2026, 8, 10),
    ]


def test_delisted_asset_requires_no_dates() -> None:
    window = AssetLifecycleWindow(asset_id="ashare:x", delisted=True)
    assert window.required_dates(TRADING_DAYS) == []


def test_bar_gap_detection_applies_lifecycle() -> None:
    covered_c = {date(2026, 8, d) for d in (3, 4, 5, 7, 10, 11)}  # 仅停牌日 8/6 缺失
    rows = [("ashare:c", day) for day in sorted(covered_c)]
    session = _FakeSession([rows])
    queries = DomainGapQueries(session)
    lifecycles = {
        "ashare:a": AssetLifecycleWindow(
            asset_id="ashare:a", list_date=date(2026, 8, 5)
        ),
        "ashare:b": AssetLifecycleWindow(asset_id="ashare:b", delisted=True),
        "ashare:c": AssetLifecycleWindow(
            asset_id="ashare:c", suspended_dates=(date(2026, 8, 6),)
        ),
    }
    targets = queries.detect_bar_gap_targets(
        asset_ids=["ashare:a", "ashare:b", "ashare:c"],
        trading_dates=TRADING_DAYS,
        lifecycles=lifecycles,
    )
    scopes = {(t.asset_id, t.gap_start_at.date(), t.gap_end_at.date()) for t in targets}
    # 上市前 8/3-8/4 不构成缺口；剩余真实缺口被压缩为两段
    assert ("ashare:a", date(2026, 8, 5), date(2026, 8, 7)) in scopes
    assert ("ashare:a", date(2026, 8, 10), date(2026, 8, 11)) in scopes
    # 退市资产不产生任何目标
    assert all(asset != "ashare:b" for asset, _, _ in scopes)
    # 停牌日不算缺口：c 无目标
    assert all(asset != "ashare:c" for asset, _, _ in scopes)
    assert len(targets) == 2
    # 注入生命周期后不再触发生命周期查询
    assert len(session.statements) == 1


def test_load_asset_lifecycles_reads_status_payload_and_suspension() -> None:
    session = _FakeSession(
        [
            [
                ("ashare:d", "delisted", {"delist_date": "2026-07-31"}),
                ("ashare:e", "available", {"list_date": "20260803"}),
            ],
            [("ashare:e", datetime(2026, 8, 6, 2, 0, tzinfo=UTC))],
        ]
    )
    windows = DomainGapQueries(session).load_asset_lifecycles(
        asset_ids=["ashare:d", "ashare:e"],
        window_start=date(2026, 8, 1),
        window_end=date(2026, 8, 31),
    )
    delisted = windows["ashare:d"]
    live = windows["ashare:e"]
    assert delisted.delisted is True
    assert delisted.delist_date == date(2026, 7, 31)
    assert live.list_date == date(2026, 8, 3)
    # 停牌快照归一到上海自然日
    assert live.suspended_dates == (date(2026, 8, 6),)



def test_delisted_asset_keeps_history_before_delist_date() -> None:
    """复审 H4-③：退市只剔除退市日之后，历史区间仍须补齐。"""

    window = AssetLifecycleWindow(
        asset_id="ashare:x",
        delist_date=date(2026, 8, 10),
        delisted=True,
    )
    assert window.required_dates(TRADING_DAYS) == list(TRADING_DAYS[:6])


def test_valuation_query_filters_authoritative_sources() -> None:
    """复审 H3：估值覆盖判定只认 spot/historical 两个权威来源。"""

    from finance_agent.data_recovery.gap_detector import VALUATION_SOURCES

    assert set(VALUATION_SOURCES) == {
        "akshare:stock_zh_a_spot",
        "akshare:stock_value_em",
    }
    session = _FakeSession([[("ashare:000001", datetime(2026, 11, 13, tzinfo=UTC))]])
    DomainGapQueries(session).detect_valuation_gap_targets(
        asset_ids=["ashare:000001"],
        required_as_of=REQUIRED_DAY,
        cutoff_at=CUTOFF_AT,
    )
    statement_sql = str(session.statements[-1])
    assert "source IN" in statement_sql


def test_fundamental_query_filters_authoritative_sources() -> None:
    """复审 MEDIUM：报告期覆盖判定只认权威财务指标来源。"""

    session = _FakeSession([[]])
    DomainGapQueries(session).detect_fundamental_gap_targets(
        asset_ids=["ashare:600000"],
        cutoff_at=CUTOFF_AT,
    )
    compiled = session.statements[-1].compile()
    assert list(FUNDAMENTAL_REPORT_SOURCES) in [
        value for value in compiled.params.values() if isinstance(value, list)
    ]


class _CaptureQueries:
    """捕获 PlanBuilder 传给估值检测的时间参数。"""

    def __init__(self) -> None:
        self.captured = {}

    def detect_bar_gap_targets(self, **kwargs):
        return []

    def detect_fundamental_gap_targets(self, *, asset_ids, cutoff_at):
        self.captured["cutoff_at"] = cutoff_at
        return []

    def detect_valuation_gap_targets(self, *, asset_ids, required_as_of, cutoff_at):
        self.captured["required_as_of"] = required_as_of
        return []

    def detect_capital_flow_gap_targets(self, **kwargs):
        return []

    def domain_watermark_latest(self, *, data_domain):
        return None

    def detect_window_gap_target(self, **kwargs):
        return None


def test_plan_builder_cutoff_at_is_shanghai_tz() -> None:
    """复审 MEDIUM：冻结截止时刻显式上海时区，不随宿主机时区漂移。"""

    from datetime import timedelta

    from finance_agent.data_recovery.gap_detector import ASHARE_TIMEZONE, CutoffResolution
    from finance_agent.data_recovery.models import UniverseSnapshot
    from finance_agent.data_recovery.plan_builder import PlanBuilder

    queries = _CaptureQueries()
    PlanBuilder(queries).build_plan(
        market="ashare",
        cutoff=CutoffResolution(
            cutoff_date=date(2026, 11, 13),
            source="database",
            calendar_fresh=True,
        ),
        universe=UniverseSnapshot(
            universe_id="universe:test",
            snapshot_at=datetime(2026, 11, 13, tzinfo=UTC),
            snapshot_hash="u1",
            asset_ids=("ashare:600000",),
        ),
        trading_dates=[date(2026, 11, 13)],
        now=datetime(2026, 11, 14, tzinfo=UTC),
    )
    cutoff_at = queries.captured["cutoff_at"]
    assert cutoff_at.tzinfo is not None
    assert cutoff_at.utcoffset() == timedelta(hours=8)
    assert cutoff_at.astimezone(ASHARE_TIMEZONE).date() == date(2026, 11, 13)
    # 估值窗口起点 = 截止时刻前一天（上海日历日）
    assert queries.captured["required_as_of"].astimezone(
        ASHARE_TIMEZONE
    ).date() == date(2026, 11, 12)


def test_default_module_wires_calendar_refresh_consumable_by_cutoff() -> None:
    """规格 6.1 装配：日历刷新回调产出可被 latest_closed_trading_date 消费。"""

    from finance_agent.data_recovery.assembly import (
        build_default_recovery_module,
    )
    from finance_agent.data_recovery.gap_detector import (
        latest_closed_trading_date,
    )

    module = build_default_recovery_module(None)
    assert module.detector.calendar_refresh is not None  # 不再是裸构造

    entries = module.detector.calendar_refresh()
    assert entries, "回调应返回非空日历条目"
    first = entries[-1]
    assert {"trade_date", "is_trading_day", "close_at", "status"} <= set(first)
    assert first["close_at"].tzinfo is not None  # 上海时区 aware
    # 规格语义：最近已收盘交易日可从回调结果推出（周五收盘后应得周五）
    now = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)
    assert latest_closed_trading_date(entries, now=now) == date(2026, 8, 21)



def test_universe_refresh_returns_asset_ids(monkeypatch) -> None:
    """规格 6.2 装配：资产池刷新回调产出 asset_ids 列表。"""

    from types import SimpleNamespace as NS

    from finance_agent.data.providers import akshare_provider as mod
    from finance_agent.data_recovery.assembly import (
        _universe_refresh,
        build_default_recovery_module,
    )

    def fake_fetch_assets(self):
        return NS(assets=[NS(asset_id="ashare:600000"), NS(asset_id=None)])

    monkeypatch.setattr(mod.AkshareProvider, "fetch_assets", fake_fetch_assets)
    payload = _universe_refresh()
    assert payload == {"asset_ids": ["ashare:600000"]}  # 无 ID 的行被过滤

    module = build_default_recovery_module(None)
    assert module.detector.universe_refresh is not None  # 装配到位
