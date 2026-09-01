"""停跑恢复缺口检测器。

只读识别各数据域的真实事实缺口：交易日历、生产资产池、闭合日 K、
基本面报告期、估值截面、资金流、新闻公告与风险情绪窗口。
所有查询都是集合查询，禁止逐资产 N+1（规格 16.6）。
检测不写入任何业务表；日历或资产池过期时通过注入的 Provider 回调做
只读刷新，失败时计划标记为不可执行，不允许按工作日猜测交易日。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_agent.data.freshness import (
    ASHARE_FINANCIAL_INDICATOR_SOURCE,
    ASHARE_TIMEZONE,
    ashare_daily_snapshot_at,
    expected_ashare_report_period,
)
from finance_agent.data_recovery.models import DataDomain, GapTarget, UniverseSnapshot
from finance_agent.storage.orm import (
    AssetORM,
    AssetStatusSnapshotORM,
    CapitalFlowSnapshotORM,
    DataSyncWatermarkORM,
    FundamentalSnapshotORM,
    MarketBarORM,
    MarketCalendarORM,
)

JsonDict = dict[str, Any]

#: K 线有效终态，与 repositories.FINAL_MARKET_BAR_STATUSES 保持一致。
FINAL_MARKET_BAR_STATUSES = ("available", "revised")

#: 生产配置默认候选池（sync_config 的 ashare 推荐候选池模式）。
DEFAULT_ASHARE_UNIVERSE_ID = "universe:base:ashare:p0:all_a"

# ---------------------------------------------------------------------------
# 资产生命周期（规格 8：上市前、退市后、停牌日不构成 K 线缺口）。
# orm.py 现状：无独立 list_date/delist_date 列；生命周期信号来自
# assets.status、assets.payload（可选日期键）与 asset_status_snapshots.
# trading_status 停牌快照。
# ---------------------------------------------------------------------------

#: 视为已终止交易（退市后）的 assets.status 取值。
DELISTED_ASSET_STATUSES = frozenset({"delisted", "terminated"})

#: 视为停牌的 asset_status_snapshots.trading_status 取值。
SUSPENDED_TRADING_STATUSES = frozenset({"suspended", "suspension", "停牌"})

#: assets.payload 中可能的上市/退市日期键（来源各异，做宽容解析）。
LIST_DATE_PAYLOAD_KEYS = ("list_date", "listing_date", "ipo_date")
DELIST_DATE_PAYLOAD_KEYS = ("delist_date", "delisting_date")


# 估值事实的权威来源（复审 H3）：股息率等无 report_period 的快照
# 不得冒充估值覆盖判定依据。
VALUATION_SOURCES: tuple[str, ...] = (
    "akshare:stock_zh_a_spot",
    "akshare:stock_value_em",
)

# 基本面报告期的权威来源（复审 MEDIUM）：与既有健康策略一致，
# 业绩报表等其它带 report_period 的快照不得提前让报告期目标完成。
FUNDAMENTAL_REPORT_SOURCES: tuple[str, ...] = (ASHARE_FINANCIAL_INDICATOR_SOURCE,)


@dataclass(frozen=True)
class AssetLifecycleWindow:
    """单个资产在补跑窗口内的有效交易日边界。

    list_date/delist_date 缺失时表示未知，不做该侧裁剪；delisted 表示
    资产已终止交易，窗口内全部交易日都不再要求 K 线。
    """

    asset_id: str
    list_date: date | None = None
    delist_date: date | None = None
    suspended_dates: tuple[date, ...] = ()
    delisted: bool = False

    def required_dates(self, trading_dates: Iterable[date]) -> list[date]:
        """返回该资产在有效期内应补的交易日子集（纯集合运算）。"""

        suspended = frozenset(self.suspended_dates)
        required: list[date] = []
        for day in sorted(set(trading_dates)):
            if self.delisted:
                # 复审 H4-③：仅剔除退市日之后的日期，退市前历史仍须补齐；
                # 无 delist_date 时保守视为全程无效。
                if self.delist_date is None or day > self.delist_date:
                    continue
            if self.delist_date is not None and day > self.delist_date:
                continue
            if self.list_date is not None and day < self.list_date:
                continue
            if day in suspended:
                continue
            required.append(day)
        return required


def _payload_date(payload: Any, keys: Sequence[str]) -> date | None:
    """从资产 payload JSON 宽容解析第一个可识别的日期键。"""

    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        parsed = report_period_to_date(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def report_period_to_date(value: Any) -> date | None:
    """解析 YYYYMMDD 或 ISO 报告期字符串；与采集脚本保持同一口径。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value or "").strip()
    if not text_value:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text_value, fmt).date()
        except ValueError:
            continue
    return None


def latest_closed_trading_date(rows: Iterable[Any], *, now: datetime) -> date | None:
    """从日历行中取最近一个已完整收盘的交易日（规格 6.1）。

    条件：market 匹配由调用方过滤；is_trading_day 为真、当日 close_at
    早于当前时间、记录状态有效。
    """

    def _field(row, name, default=None):
        # Provider 只读回调传入规范化 dict，数据库分支传入 ORM 行对象。
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)

    result: date | None = None
    for row in rows:
        if not _field(row, "is_trading_day", False):
            continue
        status = str(_field(row, "status", "") or "")
        if status == "invalid":
            continue
        close_at = _field(row, "close_at")
        if close_at is None:
            continue
        if close_at >= now:
            continue
        trade_date = _field(row, "trade_date")
        if trade_date is not None and (result is None or trade_date > result):
            result = trade_date
    return result


def compress_consecutive_dates(dates: Sequence[date]) -> list[tuple[date, date]]:
    """把有序缺失日期压缩为连续区间，休市日自动并入跨越区间。"""

    ordered = sorted(set(dates))
    ranges: list[tuple[date, date]] = []
    for current in ordered:
        if ranges and (current - ranges[-1][1]).days == 1:
            ranges[-1] = (ranges[-1][0], current)
        else:
            ranges.append((current, current))
    return ranges


def missing_trade_dates_by_asset(
    *,
    asset_ids: Sequence[str],
    trading_dates: Sequence[date],
    covered: Mapping[str, set[date]],
) -> dict[str, list[date]]:
    """按资产返回有效期内缺失的交易日集合（纯集合运算）。"""

    trading = sorted(set(trading_dates))
    missing: dict[str, list[date]] = {}
    for asset_id in asset_ids:
        have = covered.get(asset_id, set())
        gaps = [d for d in trading if d not in have]
        if gaps:
            missing[asset_id] = gaps
    return missing


@dataclass(frozen=True)
class CutoffResolution:
    """冻结截止日的解析结果。"""

    cutoff_date: date | None
    source: str
    calendar_fresh: bool
    detail: JsonDict = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        return self.cutoff_date is not None and self.calendar_fresh



class GapDetector:
    """只读缺口检测：所有方法都不写业务表。"""

    def __init__(
        self,
        session: Session,
        *,
        market: str = "ashare",
        universe_id: str = DEFAULT_ASHARE_UNIVERSE_ID,
        calendar_refresh: Callable[[], list[JsonDict]] | None = None,
        universe_refresh: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.session = session
        self.market = market
        self.universe_id = universe_id
        # 只读 Provider 回调：返回规范化数据；None 表示无法追溯。
        self.calendar_refresh = calendar_refresh
        self.universe_refresh = universe_refresh

    # ------------------------------------------------------------------
    # 冻结截止日
    # ------------------------------------------------------------------

    def resolve_cutoff(self, *, now: datetime | None = None) -> CutoffResolution:
        """解析冻结截止日：日历优先，过期时只读刷新 Provider。"""

        checked_at = now or datetime.now().astimezone()
        rows = list(
            self.session.execute(
                select(MarketCalendarORM)
                .where(
                    MarketCalendarORM.market == self.market,
                    MarketCalendarORM.trade_date >= checked_at.date() - timedelta(days=30),
                    MarketCalendarORM.trade_date <= checked_at.date(),
                )
                .order_by(MarketCalendarORM.trade_date.desc())
            ).scalars()
        )
        cutoff = latest_closed_trading_date(rows, now=checked_at)
        newest_row_date = max((row.trade_date for row in rows), default=None)
        calendar_covers_now = (
            newest_row_date is not None and newest_row_date >= checked_at.date()
        )
        if cutoff is not None and calendar_covers_now:
            return CutoffResolution(
                cutoff_date=cutoff,
                source="database",
                calendar_fresh=True,
                detail={"calendar_rows": len(rows)},
            )
        if self.calendar_refresh is not None:
            try:
                normalized = self.calendar_refresh() or []
            except Exception as exc:  # noqa: BLE001 - Provider 边界需要结构化保留失败
                return CutoffResolution(
                    cutoff_date=None,
                    source="provider_error",
                    calendar_fresh=False,
                    detail={"error": str(exc)[:200]},
                )
            entries: list[JsonDict] = []
            for item in normalized:
                trade_date = item.get("trade_date")
                if isinstance(trade_date, str):
                    trade_date = date.fromisoformat(trade_date)
                if not isinstance(trade_date, date):
                    continue
                close_at = item.get("close_at")
                if isinstance(close_at, str):
                    close_at = datetime.fromisoformat(close_at)
                entries.append(
                    {
                        "trade_date": trade_date,
                        "is_trading_day": bool(item.get("is_trading_day")),
                        "close_at": close_at,
                        "status": str(item.get("status") or ""),
                    }
                )
            cutoff = latest_closed_trading_date(entries, now=checked_at)
            if cutoff is not None:
                return CutoffResolution(
                    cutoff_date=cutoff,
                    source="provider",
                    calendar_fresh=True,
                    detail={"provider_entries": len(entries)},
                )
            return CutoffResolution(
                cutoff_date=None,
                source="provider_empty",
                calendar_fresh=False,
                detail={"provider_entries": len(entries)},
            )
        return CutoffResolution(
            cutoff_date=cutoff,
            source="database_stale" if cutoff else "database_missing",
            calendar_fresh=False,
            detail={"calendar_rows": len(rows)},
        )

    # ------------------------------------------------------------------
    # 生产资产池
    # ------------------------------------------------------------------

    def load_universe_snapshot(
        self, *, cutoff_date: date, now: datetime | None = None
    ) -> tuple[UniverseSnapshot | None, JsonDict]:
        """读取生产候选池成员并应用可交易资格规则（规格 6.2）。

        返回 (快照或 None, 诊断)。快照过期时尝试注入的只读刷新回调。
        """

        from finance_agent.application.asset_eligibility_service import (
            TradeableAssetEligibilityService,
        )
        from finance_agent.storage.repositories import UniverseRepository

        checked_at = now or datetime.now().astimezone()
        diagnostics: JsonDict = {"universe_id": self.universe_id}
        members = UniverseRepository(self.session).list_members(
            self.universe_id, included_only=True
        )
        universe_as_of = None
        asset_ids: list[str] = []
        if members:
            # 完整实体查询：资格服务需要 symbol/asset_type 等属性，
            # 只选 4 列的 Row 对象缺字段会导致全部成员被拒。
            statement = select(AssetORM).where(
                AssetORM.asset_id.in_([member.asset_id for member in members])
            )
            asset_rows = {
                row.asset_id: row
                for row in self.session.execute(statement).scalars()
            }
            eligibility = TradeableAssetEligibilityService()
            for member in members:
                row = asset_rows.get(member.asset_id)
                if row is None:
                    continue
                # 直接传 ORM 行：资格服务按属性读取 symbol/asset_type 等字段，
                # 旧 dict 只有 4 个键导致 symbol 恒为空、全部成员被拒
                # （eligible=0，复审实机验证发现）。
                if eligibility.is_tradeable_asset(row):
                    asset_ids.append(row.asset_id)
            universe_as_of = max(
                (member.as_of for member in members if getattr(member, "as_of", None)),
                default=None,
            )
        stale_after_days = 5
        snapshot_stale = (
            universe_as_of is None
            or universe_as_of.astimezone().date() < cutoff_date - timedelta(days=stale_after_days)
        )
        diagnostics["member_count"] = len(members)
        diagnostics["eligible_count"] = len(asset_ids)
        diagnostics["snapshot_stale"] = snapshot_stale
        if snapshot_stale and self.universe_refresh is not None:
            try:
                refreshed = self.universe_refresh()
            except Exception as exc:  # noqa: BLE001
                diagnostics["refresh_error"] = str(exc)[:200]
                refreshed = None
            if refreshed and refreshed.get("asset_ids"):
                asset_ids = [str(item) for item in refreshed["asset_ids"]]
                diagnostics["source"] = "provider"
            else:
                diagnostics["source"] = "provider_unavailable"
        elif snapshot_stale:
            diagnostics["source"] = "stale_without_provider"
        else:
            diagnostics["source"] = "database"
        if not asset_ids:
            return None, diagnostics
        ordered = sorted(set(asset_ids))
        digest = hashlib_sha256("|".join([self.universe_id, *ordered]))
        snapshot_at = refreshed_at_or(universe_as_of, checked_at)
        return (
            UniverseSnapshot(
                universe_id=self.universe_id,
                snapshot_at=snapshot_at,
                snapshot_hash=digest,
                asset_ids=tuple(ordered),
                source=str(diagnostics.get("source") or "database"),
            ),
            diagnostics,
        )



# ----------------------------------------------------------------------
# 小工具（供上方方法使用）
# ----------------------------------------------------------------------


def hashlib_sha256(material: str) -> str:
    """sha256 摘要的短格式。"""

    import hashlib

    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def refreshed_at_or(value: datetime | None, fallback: datetime) -> datetime:
    return value or fallback


def _day_bounds(trade_date: date, tz: Any = None) -> tuple[datetime, datetime]:
    """A 股交易日的 UTC 时间边界（按上海时区零点到次日零点）。"""

    start = datetime.combine(trade_date, time.min, tzinfo=ASHARE_TIMEZONE)
    end = start + timedelta(days=1)
    return start, end


class DomainGapQueries:
    """各数据域事实覆盖查询：全部为集合查询，返回压缩后的目标区间。"""

    def __init__(self, session: Session, *, market: str = "ashare") -> None:
        self.session = session
        self.market = market

    # -- 闭合日 K -------------------------------------------------------

    def bar_coverage_by_asset(
        self,
        *,
        asset_ids: Sequence[str],
        start_at: datetime,
        end_at: datetime,
        timeframe: str = "1d",
    ) -> dict[str, set[date]]:
        """一次分组查询返回 asset_id -> 已收盘 K 线日期集合。"""

        if not asset_ids:
            return {}
        statement = (
            select(MarketBarORM.asset_id, func.date(MarketBarORM.timestamp))
            .where(
                MarketBarORM.asset_id.in_(list(asset_ids)),
                MarketBarORM.timeframe == timeframe,
                MarketBarORM.is_closed.is_(True),
                MarketBarORM.status.in_(FINAL_MARKET_BAR_STATUSES),
                MarketBarORM.timestamp >= start_at,
                MarketBarORM.timestamp < end_at,
            )
            .group_by(MarketBarORM.asset_id, func.date(MarketBarORM.timestamp))
        )
        coverage: dict[str, set[date]] = {}
        for asset_id, day in self.session.execute(statement).all():
            if day is None:
                continue
            coverage.setdefault(str(asset_id), set()).add(
                day if isinstance(day, date) else datetime.fromisoformat(str(day)).date()
            )
        return coverage

    def load_asset_lifecycles(
        self,
        *,
        asset_ids: Sequence[str],
        window_start: date | None = None,
        window_end: date | None = None,
    ) -> dict[str, AssetLifecycleWindow]:
        """集合查询资产生命周期（规格 16.6：禁止逐资产 N+1）。

        assets.status/payload 给出上市、退市边界；asset_status_snapshots
        的停牌快照按上海自然日并入 suspended_dates。窗口边界只用于裁剪
        停牌快照的读取范围。
        """

        ids = [str(item) for item in dict.fromkeys(asset_ids)]
        if not ids:
            return {}
        lifecycles: dict[str, AssetLifecycleWindow] = {}
        for asset_id, status, payload in self.session.execute(
            select(AssetORM.asset_id, AssetORM.status, AssetORM.payload).where(
                AssetORM.asset_id.in_(ids)
            )
        ).all():
            normalized_status = str(status or '').strip().lower()
            lifecycles[str(asset_id)] = AssetLifecycleWindow(
                asset_id=str(asset_id),
                list_date=_payload_date(payload, LIST_DATE_PAYLOAD_KEYS),
                delist_date=_payload_date(payload, DELIST_DATE_PAYLOAD_KEYS),
                delisted=normalized_status in DELISTED_ASSET_STATUSES,
            )
        suspension_statement = select(
            AssetStatusSnapshotORM.asset_id,
            AssetStatusSnapshotORM.as_of,
        ).where(
            AssetStatusSnapshotORM.asset_id.in_(ids),
            AssetStatusSnapshotORM.trading_status.in_(
                sorted(SUSPENDED_TRADING_STATUSES)
            ),
        )
        if window_start is not None:
            suspension_statement = suspension_statement.where(
                AssetStatusSnapshotORM.as_of >= datetime.combine(
                    window_start, time.min, tzinfo=ASHARE_TIMEZONE
                ) - timedelta(days=1)
            )
        if window_end is not None:
            suspension_statement = suspension_statement.where(
                AssetStatusSnapshotORM.as_of <= datetime.combine(
                    window_end, time.max, tzinfo=ASHARE_TIMEZONE
                )
            )
        suspended_by_asset: dict[str, set[date]] = {}
        for asset_id, as_of in self.session.execute(suspension_statement).all():
            if not isinstance(as_of, datetime):
                continue
            day = ashare_daily_snapshot_at(as_of).date()
            if window_start is not None and day < window_start:
                continue
            if window_end is not None and day > window_end:
                continue
            suspended_by_asset.setdefault(str(asset_id), set()).add(day)
        for asset_id in ids:
            window = lifecycles.get(asset_id) or AssetLifecycleWindow(asset_id=asset_id)
            suspended = tuple(sorted(suspended_by_asset.get(asset_id, ())))
            lifecycles[asset_id] = (
                window if not suspended
                else replace(window, suspended_dates=suspended)
            )
        return lifecycles

    def detect_bar_gap_targets(
        self,
        *,
        asset_ids: Sequence[str],
        trading_dates: Sequence[date],
        timeframe: str = "1d",
        lifecycles: Mapping[str, AssetLifecycleWindow] | None = None,
    ) -> list[GapTarget]:
        """资产有效期内的缺失交易日 → 压缩区间目标（规格 8）。

        应用资产生命周期：上市日之前、退市日之后与停牌日不构成缺口；
        lifecycles 可注入以避免重复查询，缺省时集合加载一次。
        """

        trading = sorted(set(trading_dates))
        ordered_ids = [str(item) for item in dict.fromkeys(asset_ids)]
        if not trading or not ordered_ids:
            return []
        windows = (
            lifecycles
            if lifecycles is not None
            else self.load_asset_lifecycles(
                asset_ids=ordered_ids, window_start=trading[0], window_end=trading[-1]
            )
        )
        start_at, _ = _day_bounds(trading[0])
        _, end_at = _day_bounds(trading[-1])
        end_at = end_at + timedelta(days=1)
        covered = self.bar_coverage_by_asset(
            asset_ids=ordered_ids, start_at=start_at, end_at=end_at, timeframe=timeframe
        )
        targets: list[GapTarget] = []
        for asset_id in ordered_ids:
            window = windows.get(asset_id) or AssetLifecycleWindow(asset_id=asset_id)
            have = covered.get(asset_id, set())
            missing = [day for day in window.required_dates(trading) if day not in have]
            for gap_start, gap_end in compress_consecutive_dates(missing):
                targets.append(
                    GapTarget(
                        data_domain=DataDomain.MARKET_BARS,
                        gap_start_at=datetime.combine(gap_start, time.min, tzinfo=ASHARE_TIMEZONE),
                        gap_end_at=datetime.combine(gap_end, time.min, tzinfo=ASHARE_TIMEZONE),
                        granularity=timeframe,
                        asset_id=asset_id,
                        expected_count=(gap_end - gap_start).days + 1,
                    )
                )
        return targets



    # -- 基本面报告期 ---------------------------------------------------

    def detect_fundamental_gap_targets(
        self, *, asset_ids: Sequence[str], cutoff_at: datetime
    ) -> list[GapTarget]:
        """截止日依法应有的最低报告期（规格 6.3/8）。

        缺口按资产粒度生成：每个报告期落后的冻结池成员各得到一个
        asset_id 非空的目标，保证 verifier 能按真实资产 ID 复核事实
        （规格 11.3；市场级 asset_id=None 目标会形成 IN (NULL)，
        永远无法完成事实验证）。data_domain=fundamentals 与目标 scope
        保持确定性。
        """

        if not asset_ids:
            return []
        expected = expected_ashare_report_period(cutoff_at)
        rows = self.session.execute(
            select(
                FundamentalSnapshotORM.asset_id,
                func.max(FundamentalSnapshotORM.report_period),
            )
            .where(
                FundamentalSnapshotORM.asset_id.in_(list(asset_ids)),
                FundamentalSnapshotORM.report_period.is_not(None),
                FundamentalSnapshotORM.source.in_(FUNDAMENTAL_REPORT_SOURCES),
            )
            .group_by(FundamentalSnapshotORM.asset_id)
        ).all()
        latest_by_asset = {
            str(asset_id): report_period_to_date(value) for asset_id, value in rows
        }
        targets: list[GapTarget] = []
        for asset_id in asset_ids:
            latest = latest_by_asset.get(str(asset_id))
            if latest is not None and latest >= expected:
                continue
            targets.append(
                GapTarget(
                    data_domain=DataDomain.FUNDAMENTALS,
                    gap_start_at=datetime.combine(expected, time.min, tzinfo=ASHARE_TIMEZONE),
                    gap_end_at=cutoff_at,
                    granularity="report",
                    asset_id=str(asset_id),
                    expected_count=1,
                    exception_evidence={
                        "expected_report_period": expected.isoformat(),
                        "latest_report_period": latest.isoformat() if latest else None,
                    },
                )
            )
        return targets

    # -- 估值截面 -------------------------------------------------------

    def detect_valuation_gap_targets(
        self,
        *,
        asset_ids: Sequence[str],
        required_as_of: datetime,
        cutoff_at: datetime,
    ) -> list[GapTarget]:
        """最新有效截面早于窗口起点或缺记录的资产 → 单资产快照目标（规格 8）。

        从冻结资产池全体成员出发：从未写入过估值快照的资产同样进入
        缺口集合，不能只从既有 fundamental_snapshots 行反推。目标携带
        required_as_of 证据，验证方以计划要求的窗口起点判定补齐，
        而不是目标行携带的旧记录时间（规格 11.3）。
        """

        if not asset_ids:
            return []
        required_day = ashare_daily_snapshot_at(required_as_of)
        rows = self.session.execute(
            select(
                FundamentalSnapshotORM.asset_id,
                func.max(FundamentalSnapshotORM.as_of),
            )
            .where(
                FundamentalSnapshotORM.asset_id.in_(list(asset_ids)),
                FundamentalSnapshotORM.report_period.is_(None),
                FundamentalSnapshotORM.source.in_(VALUATION_SOURCES),
            )
            .group_by(FundamentalSnapshotORM.asset_id)
        ).all()
        latest_by_asset = {
            str(asset_id): ashare_daily_snapshot_at(latest_as_of) if latest_as_of else None
            for asset_id, latest_as_of in rows
        }
        targets: list[GapTarget] = []
        for asset_id in asset_ids:
            normalized = latest_by_asset.get(str(asset_id))
            if normalized is not None and normalized >= required_day:
                continue
            targets.append(
                GapTarget(
                    data_domain=DataDomain.VALUATION,
                    gap_start_at=normalized or required_day,
                    gap_end_at=cutoff_at,
                    granularity="snapshot",
                    asset_id=str(asset_id),
                    expected_count=1,
                    exception_evidence={
                        "required_as_of": required_day.isoformat(),
                    },
                )
            )
        return targets



    # -- 资金流 ---------------------------------------------------------

    def detect_capital_flow_gap_targets(
        self,
        *,
        asset_ids: Sequence[str],
        trading_dates: Sequence[date],
    ) -> list[GapTarget]:
        """资金流按交易日覆盖检测；窗口受源端历史保留期约束由调用方裁剪。"""

        trading = sorted(set(trading_dates))
        if not trading or not asset_ids:
            return []
        start_at, _ = _day_bounds(trading[0])
        _, end_at = _day_bounds(trading[-1])
        end_at = end_at + timedelta(days=1)
        rows = self.session.execute(
            select(
                CapitalFlowSnapshotORM.asset_id,
                func.date(CapitalFlowSnapshotORM.as_of),
            )
            .where(
                CapitalFlowSnapshotORM.asset_id.in_(list(asset_ids)),
                CapitalFlowSnapshotORM.as_of >= start_at,
                CapitalFlowSnapshotORM.as_of < end_at,
                CapitalFlowSnapshotORM.status != "invalid",
            )
            .group_by(CapitalFlowSnapshotORM.asset_id, func.date(CapitalFlowSnapshotORM.as_of))
        ).all()
        covered: dict[str, set[date]] = {}
        for asset_id, day in rows:
            if day is None:
                continue
            covered.setdefault(str(asset_id), set()).add(
                day if isinstance(day, date) else datetime.fromisoformat(str(day)).date()
            )
        missing_map = missing_trade_dates_by_asset(
            asset_ids=asset_ids, trading_dates=trading, covered=covered
        )
        targets: list[GapTarget] = []
        for asset_id in sorted(missing_map):
            for gap_start, gap_end in compress_consecutive_dates(missing_map[asset_id]):
                targets.append(
                    GapTarget(
                        data_domain=DataDomain.CAPITAL_FLOW,
                        gap_start_at=datetime.combine(gap_start, time.min, tzinfo=ASHARE_TIMEZONE),
                        gap_end_at=datetime.combine(gap_end, time.min, tzinfo=ASHARE_TIMEZONE),
                        granularity="1d",
                        asset_id=asset_id,
                        expected_count=(gap_end - gap_start).days + 1,
                    )
                )
        return targets

    # -- 新闻公告与风险情绪（市场级窗口） --------------------------------

    def latest_collected_at(self, *, orm_model: Any) -> datetime | None:
        return self.session.execute(select(func.max(orm_model.collected_at))).scalar_one_or_none()

    def detect_window_gap_target(
        self,
        *,
        data_domain: str,
        gap_start_at: datetime,
        cutoff_at: datetime,
        watermark_latest: datetime | None,
        stale_tolerance: timedelta,
    ) -> GapTarget | None:
        """事件/风险类市场级窗口目标：事实覆盖或水位落后即产生窗口目标。

        空响应不解释为无事件；只有窗口完全被既有事实覆盖时才返回 None。
        """

        reference = watermark_latest or gap_start_at
        if reference >= cutoff_at - stale_tolerance and watermark_latest is not None:
            return None
        return GapTarget(
            data_domain=data_domain,
            gap_start_at=gap_start_at,
            gap_end_at=cutoff_at,
            granularity="window",
            asset_id=None,
            expected_count=0,
            exception_evidence={
                "watermark_latest": watermark_latest.isoformat() if watermark_latest else None,
                "stale_tolerance_seconds": int(stale_tolerance.total_seconds()),
            },
        )

    def domain_watermark_latest(self, *, data_domain: str) -> datetime | None:
        row = self.session.execute(
            select(func.max(DataSyncWatermarkORM.watermark_at)).where(
                DataSyncWatermarkORM.data_domain == data_domain
            )
        ).scalar_one_or_none()
        return row
    def domain_watermark_earliest(self, *, data_domain: str) -> datetime | None:
        """域内最早水位（复审 H4-②）：单个新水位不得掩盖旧资产水位。"""

        row = self.session.execute(
            select(func.min(DataSyncWatermarkORM.watermark_at)).where(
                DataSyncWatermarkORM.data_domain == data_domain
            )
        ).scalar_one_or_none()
        return row
