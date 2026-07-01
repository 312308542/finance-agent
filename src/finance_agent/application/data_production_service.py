"""数据层生产化策略服务。

这里集中放置不依赖外部接口的生产化规则，方便调度器、CLI、TUI 和后续页面复用：
- 交易日历构造与交易日判断。
- 数据质量缺口转补采任务。
- Binance 限流识别和备用端点选择。
- 同市场候选池合并。
- 回避池生成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from finance_agent.storage.repositories import AssetRepository, RiskRepository, UniverseRepository

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class MarketCalendarEntry:
    """可直接写入 `market_calendars` 的交易日历条目。"""

    calendar_id: str
    market: str
    exchange: str
    trade_date: date
    is_trading_day: bool
    open_at: datetime | None
    close_at: datetime | None
    session_type: str
    timezone: str
    source: str
    status: str = "available"
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class BackfillJobPlan:
    """由健康检查缺口推导出的补采任务。"""

    task_key: str
    market: str
    task_type: str
    group: str | tuple[str, ...]
    reason: str
    priority: int = 50
    params: JsonDict = field(default_factory=dict)
    data_packages: tuple[str, ...] = ()

    def to_scheduler_job(self, *, interval_seconds: int = 300, batch_size: int = 200) -> JsonDict:
        """转换为基础数据调度器可识别的 job 结构。"""

        return {
            "name": self.task_key,
            "group": self.group,
            "enabled": True,
            "interval_seconds": interval_seconds,
            "limit": batch_size,
            "market": self.market,
            "params": {
                "sync_task_type": self.task_type,
                "mode": "gap_backfill",
                "data_packages": list(self.data_packages),
                **self.params,
            },
        }


@dataclass(frozen=True)
class BinanceRetryDecision:
    """Binance 限流后的重试决策。"""

    should_retry: bool
    is_rate_limited: bool
    next_base_url: str | None
    retry_after_seconds: float
    reason: str


@dataclass(frozen=True)
class UniverseMemberPlan:
    """候选池成员写入计划。"""

    member_id: str
    universe_id: str
    asset_id: str
    symbol: str
    market: str
    as_of: datetime
    included: bool = True
    removed_reason: str | None = None
    rank_hint: int | None = None
    payload: JsonDict = field(default_factory=dict)

    def to_repository_payload(self) -> JsonDict:
        """转换为 `UniverseRepository.replace_members` 的成员参数。"""

        return {
            "member_id": self.member_id,
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "market": self.market,
            "as_of": self.as_of,
            "included": self.included,
            "removed_reason": self.removed_reason,
            "rank_hint": self.rank_hint,
            "payload": self.payload,
        }


class MarketCalendarService:
    """交易日历构造与查询服务。"""

    ashare_timezone = "Asia/Shanghai"

    def build_ashare_calendar_entries(
        self,
        *,
        trading_dates: list[date],
        start_date: date,
        end_date: date,
        source: str,
        exchange: str = "CN",
    ) -> list[MarketCalendarEntry]:
        """根据交易日源补齐 A 股交易日和休市日。"""

        if end_date < start_date:
            raise ValueError("end_date 不能早于 start_date")
        trading_set = set(trading_dates)
        entries: list[MarketCalendarEntry] = []
        current = start_date
        while current <= end_date:
            is_trading_day = current in trading_set
            entries.append(
                self._build_ashare_entry(
                    trade_date=current,
                    is_trading_day=is_trading_day,
                    source=source,
                    exchange=exchange,
                )
            )
            current += timedelta(days=1)
        return entries

    def build_crypto_calendar_entries(
        self,
        *,
        start_date: date,
        end_date: date,
        market: str,
        source: str,
        exchange: str = "Binance",
    ) -> list[MarketCalendarEntry]:
        """构造数字货币 7x24 交易日历。"""

        if end_date < start_date:
            raise ValueError("end_date 不能早于 start_date")
        entries: list[MarketCalendarEntry] = []
        current = start_date
        while current <= end_date:
            open_at = datetime.combine(current, time.min, tzinfo=UTC)
            close_at = datetime.combine(current, time.max, tzinfo=UTC)
            entries.append(
                MarketCalendarEntry(
                    calendar_id=f"calendar:{market}:{exchange}:{current.isoformat()}:regular",
                    market=market,
                    exchange=exchange,
                    trade_date=current,
                    is_trading_day=True,
                    open_at=open_at,
                    close_at=close_at,
                    session_type="regular",
                    timezone="UTC",
                    source=source,
                    payload={"trading_model": "24x7"},
                )
            )
            current += timedelta(days=1)
        return entries

    @staticmethod
    def is_trading_day(entries: list[MarketCalendarEntry], target_date: date) -> bool:
        """在一组交易日历条目中判断目标日期是否可交易。"""

        for entry in entries:
            if entry.trade_date == target_date:
                return entry.is_trading_day
        return False

    @staticmethod
    def missing_trading_dates(
        entries: list[MarketCalendarEntry],
        existing_dates: set[date],
    ) -> list[date]:
        """按交易日历计算缺失 K 线日期，休市日不会进入补采范围。"""

        return [
            entry.trade_date
            for entry in sorted(entries, key=lambda item: item.trade_date)
            if entry.is_trading_day and entry.trade_date not in existing_dates
        ]

    def _build_ashare_entry(
        self,
        *,
        trade_date: date,
        is_trading_day: bool,
        source: str,
        exchange: str,
    ) -> MarketCalendarEntry:
        """构造单个 A 股交易日历条目。"""

        tz = ZoneInfo(self.ashare_timezone)
        open_at = (
            datetime.combine(trade_date, time(hour=9, minute=30), tzinfo=tz)
            if is_trading_day
            else None
        )
        close_at = (
            datetime.combine(trade_date, time(hour=15), tzinfo=tz)
            if is_trading_day
            else None
        )
        return MarketCalendarEntry(
            calendar_id=f"calendar:ashare:{exchange}:{trade_date.isoformat()}:regular",
            market="ashare",
            exchange=exchange,
            trade_date=trade_date,
            is_trading_day=is_trading_day,
            open_at=open_at,
            close_at=close_at,
            session_type="regular",
            timezone=self.ashare_timezone,
            source=source,
            status="available" if is_trading_day else "closed",
            payload={"trading_model": "exchange_calendar"},
        )


class DataBackfillPlanner:
    """把健康检查缺口转换为调度器补采任务。"""

    table_to_jobs: dict[str, tuple[BackfillJobPlan, ...]] = {
        "market_bars": (
            BackfillJobPlan(
                task_key="ashare_market_bars_backfill",
                market="ashare",
                task_type="market_bars_backfill",
                group="ashare-p0",
                reason="A 股行情 K 线缺口或过期",
                priority=100,
                data_packages=("market_bars",),
                params={"group": ["ashare-p0"]},
            ),
            BackfillJobPlan(
                task_key="crypto_spot_market_bars_backfill",
                market="crypto_spot",
                task_type="market_bars_backfill",
                group="crypto",
                reason="数字货币现货 K 线缺口或过期",
                priority=95,
                data_packages=("market_bars",),
                params={"group": ["crypto"], "crypto_market_type": "spot"},
            ),
            BackfillJobPlan(
                task_key="crypto_future_market_bars_backfill",
                market="crypto_future",
                task_type="market_bars_backfill",
                group="crypto",
                reason="数字货币合约 K 线缺口或过期",
                priority=95,
                data_packages=("market_bars",),
                params={"group": ["crypto"], "crypto_market_type": "future"},
            ),
        ),
        "market_calendars": (
            BackfillJobPlan(
                task_key="ashare_calendar_refresh",
                market="ashare",
                task_type="calendar_refresh",
                group="ashare-p0",
                reason="A 股交易日历缺口或过期",
                priority=105,
                data_packages=("market_calendar",),
                params={"group": ["ashare-p0"]},
            ),
            BackfillJobPlan(
                task_key="crypto_spot_calendar_refresh",
                market="crypto_spot",
                task_type="calendar_refresh",
                group="crypto",
                reason="数字货币现货 7x24 日历缺口或过期",
                priority=100,
                data_packages=("market_calendar",),
                params={"group": ["crypto"], "crypto_market_type": "spot"},
            ),
            BackfillJobPlan(
                task_key="crypto_future_calendar_refresh",
                market="crypto_future",
                task_type="calendar_refresh",
                group="crypto",
                reason="数字货币合约 7x24 日历缺口或过期",
                priority=100,
                data_packages=("market_calendar",),
                params={"group": ["crypto"], "crypto_market_type": "future"},
            ),
        ),
        "capital_flow_snapshots": (
            BackfillJobPlan(
                task_key="ashare_capital_flow_backfill",
                market="ashare",
                task_type="capital_flow_refresh",
                group="ashare-p1",
                reason="资金流快照缺口或过期",
                priority=80,
                data_packages=("capital_flow",),
                params={"group": ["ashare-p1"]},
            ),
        ),
        "fundamental_snapshots": (
            BackfillJobPlan(
                task_key="ashare_fundamental_backfill",
                market="ashare",
                task_type="fundamental_refresh",
                group="ashare-p2",
                reason="基本面/估值快照缺口或过期",
                priority=70,
                data_packages=("fundamentals", "valuation"),
                params={"group": ["ashare-p2"]},
            ),
        ),
        "event_records": (
            BackfillJobPlan(
                task_key="ashare_event_refresh",
                market="ashare",
                task_type="event_refresh",
                group="ashare-p1",
                reason="新闻公告事件缺口或过期",
                priority=60,
                data_packages=("events",),
                params={"group": ["ashare-p1"]},
            ),
        ),
        "risk_findings": (
            BackfillJobPlan(
                task_key="ashare_risk_refresh",
                market="ashare",
                task_type="risk_sentiment_refresh",
                group="ashare-risk",
                reason="风险发现缺口或风险源失败",
                priority=90,
                data_packages=("risk_sentiment",),
                params={"group": ["ashare-risk"]},
            ),
        ),
        "crypto_derivative_snapshots": (
            BackfillJobPlan(
                task_key="crypto_derivative_backfill",
                market="crypto_future",
                task_type="derivative_refresh",
                group="crypto",
                reason="数字货币衍生品快照缺口或过期",
                priority=80,
                data_packages=("derivatives",),
                params={"group": ["crypto"]},
            ),
        ),
    }

    def build_backfill_jobs(
        self,
        *,
        health_summary: JsonDict,
        now: datetime | None = None,
    ) -> list[BackfillJobPlan]:
        """从健康检查摘要生成去重后的补采任务。"""

        _ = now or datetime.now(tz=UTC)
        candidates: dict[str, BackfillJobPlan] = {}
        for hint in health_summary.get("refresh_hints", []) or []:
            table_name = str(hint.get("table_name") or "")
            self._add_candidate(candidates, table_name, reason=hint.get("reason"))
        for gap in health_summary.get("gaps", []) or []:
            gap_text = str(gap)
            for table_name in self.table_to_jobs:
                if table_name in gap_text:
                    self._add_candidate(candidates, table_name, reason=gap_text)
            if any(keyword in gap_text for keyword in ("停复牌", "退市", "风险", "fallback")):
                self._add_candidate(candidates, "risk_findings", reason=gap_text)
        return sorted(candidates.values(), key=lambda item: (-item.priority, item.task_key))

    def _add_candidate(
        self,
        candidates: dict[str, BackfillJobPlan],
        table_name: str,
        *,
        reason: Any,
    ) -> None:
        """按表名加入补采候选，保留最高优先级版本。"""

        templates = self.table_to_jobs.get(table_name)
        if templates is None:
            return
        for template in templates:
            candidates[template.task_key] = BackfillJobPlan(
                task_key=template.task_key,
                market=template.market,
                task_type=template.task_type,
                group=template.group,
                reason=str(reason or template.reason),
                priority=template.priority,
                params=dict(template.params),
                data_packages=template.data_packages,
            )


class BinanceRateLimitPolicy:
    """Binance 限流和备用端点策略。"""

    rate_limit_markers = (
        "429",
        "418",
        "too many requests",
        "rate limit",
        "retry after",
        "ip banned",
        "ddos",
        "-1003",
    )

    def __init__(
        self,
        *,
        base_urls: tuple[str, ...] = (
            "https://fapi.binance.com",
            "https://fapi1.binance.com",
            "https://fapi2.binance.com",
            "https://fapi3.binance.com",
        ),
        default_retry_after_seconds: float = 1.0,
    ) -> None:
        if not base_urls:
            raise ValueError("base_urls 不能为空")
        self.base_urls = tuple(url.rstrip("/") for url in base_urls)
        self.default_retry_after_seconds = default_retry_after_seconds

    def is_rate_limited(self, error: BaseException | str) -> bool:
        """识别 Binance 限流、临时封禁和 DDoS 保护类错误。"""

        text = str(error).lower()
        return any(marker in text for marker in self.rate_limit_markers)

    def plan_retry(
        self,
        error: BaseException | str,
        *,
        current_base_url: str,
        attempt: int,
        retry_after_header: str | None = None,
    ) -> BinanceRetryDecision:
        """根据错误和当前端点选择下一次重试方式。"""

        is_rate_limited = self.is_rate_limited(error)
        if not is_rate_limited:
            return BinanceRetryDecision(
                should_retry=False,
                is_rate_limited=False,
                next_base_url=None,
                retry_after_seconds=0,
                reason=str(error),
            )
        retry_after = self._parse_retry_after(retry_after_header)
        current = current_base_url.rstrip("/")
        if current in self.base_urls:
            index = self.base_urls.index(current)
            next_base_url = self.base_urls[(index + 1) % len(self.base_urls)]
        else:
            next_base_url = self.base_urls[0]
        return BinanceRetryDecision(
            should_retry=attempt < len(self.base_urls),
            is_rate_limited=True,
            next_base_url=next_base_url,
            retry_after_seconds=retry_after,
            reason=str(error),
        )

    def _parse_retry_after(self, value: str | None) -> float:
        """解析 Retry-After 响应头，缺失时使用默认退避。"""

        if value is None:
            return self.default_retry_after_seconds
        try:
            parsed = float(value)
        except ValueError:
            return self.default_retry_after_seconds
        return max(parsed, self.default_retry_after_seconds)


class UniverseMergeService:
    """同市场多候选池合并服务。"""

    def merge_members(
        self,
        *,
        target_universe_id: str,
        market: str,
        sources: list[JsonDict],
        as_of: datetime,
    ) -> list[UniverseMemberPlan]:
        """按来源权重合并同市场候选池成员。"""

        if market == "mixed":
            raise ValueError("候选池合并不能使用 mixed 市场")
        merged: dict[str, JsonDict] = {}
        for source in sources:
            source_market = str(source.get("market") or market)
            if source_market != market:
                raise ValueError(f"候选池来源 {source.get('universe_id')} 属于 {source_market}")
            weight = float(source.get("weight", 1.0) or 1.0)
            source_id = str(source.get("universe_id") or source.get("source") or "unknown")
            source_name = str(source.get("source") or source_id)
            for index, member in enumerate(source.get("members", []) or [], start=1):
                member_market = str(member.get("market") or market)
                if member_market != market:
                    raise ValueError(f"候选池成员 {member.get('asset_id')} 属于 {member_market}")
                asset_id = str(member.get("asset_id") or "").strip()
                symbol = str(member.get("symbol") or "").strip()
                if not asset_id or not symbol:
                    continue
                rank_hint = _safe_positive_int(member.get("rank_hint"), default=index)
                contribution = weight / max(rank_hint, 1)
                current = merged.setdefault(
                    asset_id,
                    {
                        "asset_id": asset_id,
                        "symbol": symbol,
                        "market": market,
                        "score": 0.0,
                        "best_rank": rank_hint,
                        "source_universes": [],
                        "source_weights": {},
                    },
                )
                current["score"] += contribution
                current["best_rank"] = min(int(current["best_rank"]), rank_hint)
                current["source_universes"].append(source_id)
                current["source_weights"][source_name] = weight
        ordered = sorted(
            merged.values(),
            key=lambda item: (-float(item["score"]), int(item["best_rank"]), str(item["symbol"])),
        )
        return [
            UniverseMemberPlan(
                member_id=f"universe_member:{target_universe_id}:{item['asset_id']}",
                universe_id=target_universe_id,
                asset_id=str(item["asset_id"]),
                symbol=str(item["symbol"]),
                market=market,
                as_of=as_of,
                included=True,
                rank_hint=index,
                payload={
                    "merge_score": round(float(item["score"]), 6),
                    "best_rank": item["best_rank"],
                    "source_universes": item["source_universes"],
                    "source_weights": item["source_weights"],
                },
            )
            for index, item in enumerate(ordered, start=1)
        ]


class AvoidPoolPolicy:
    """根据资产状态和风险发现生成回避池成员。"""

    blocked_statuses = {"suspended", "delisted", "terminated", "paused", "untradable"}
    high_risk_severities = {"high", "critical"}
    high_risk_types = {
        "trading_status",
        "delist_risk",
        "st_risk",
        "suspension",
        "regulatory_risk",
        "pledge_ratio",
    }

    def build_avoid_members(
        self,
        *,
        universe_id: str,
        market: str,
        assets: list[JsonDict],
        risks: list[JsonDict],
        as_of: datetime,
    ) -> list[UniverseMemberPlan]:
        """生成回避池成员，成员写入时应使用 `included=False`。"""

        reasons_by_asset: dict[str, list[str]] = {}
        for asset in assets:
            asset_id = str(asset.get("asset_id") or "").strip()
            if not asset_id:
                continue
            reasons = self._asset_reasons(asset)
            if reasons:
                reasons_by_asset.setdefault(asset_id, []).extend(reasons)
        for risk in risks:
            asset_id = str(risk.get("asset_id") or "").strip()
            if not asset_id:
                continue
            severity = str(risk.get("severity") or "").lower()
            risk_type = str(risk.get("risk_type") or "").lower()
            title = str(risk.get("title") or risk_type or "高风险发现").strip()
            if severity in self.high_risk_severities or risk_type in self.high_risk_types:
                reasons_by_asset.setdefault(asset_id, []).append(title)

        asset_by_id = {str(asset.get("asset_id")): asset for asset in assets}
        plans: list[UniverseMemberPlan] = []
        for index, (asset_id, reasons) in enumerate(sorted(reasons_by_asset.items()), start=1):
            asset = asset_by_id.get(asset_id, {})
            symbol = str(asset.get("symbol") or asset_id.rsplit(":", maxsplit=1)[-1])
            plans.append(
                UniverseMemberPlan(
                    member_id=f"universe_member:{universe_id}:{asset_id}",
                    universe_id=universe_id,
                    asset_id=asset_id,
                    symbol=symbol,
                    market=market,
                    as_of=as_of,
                    included=False,
                    removed_reason="；".join(dict.fromkeys(reasons)),
                    rank_hint=index,
                    payload={
                        "avoid_reasons": list(dict.fromkeys(reasons)),
                        "policy": "data_production_avoid_pool_v1",
                    },
                )
            )
        return plans

    def _asset_reasons(self, asset: JsonDict) -> list[str]:
        """根据资产主数据提取回避原因。"""

        reasons: list[str] = []
        name = str(asset.get("name") or "")
        status = str(asset.get("status") or "").lower()
        tradable = bool(asset.get("tradable", True))
        if status in self.blocked_statuses or not tradable:
            reasons.append("不可交易或交易状态异常")
        normalized_name = name.upper().replace(" ", "")
        if "ST" in normalized_name or "退" in name:
            reasons.append("ST/风险警示名称")
        return reasons


def _safe_positive_int(value: Any, *, default: int) -> int:
    """安全解析正整数。"""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class ProductionUniverseService:
    """候选池合并和回避池落库服务。"""

    def __init__(self, session: Session) -> None:
        self.assets = AssetRepository(session)
        self.universes = UniverseRepository(session)
        self.risks = RiskRepository(session)
        self.merge_service = UniverseMergeService()
        self.avoid_policy = AvoidPoolPolicy()

    def merge_universes(
        self,
        *,
        target_universe_id: str,
        name: str,
        source_universe_ids: list[str],
        source_weights: dict[str, float] | None = None,
        strategy_context: str,
        as_of: datetime | None = None,
    ) -> list[UniverseMemberPlan]:
        """把同市场多个候选池合并成一个候选池。"""

        as_of = as_of or datetime.now(tz=UTC)
        universes = self.universes.list_universes(source_universe_ids)
        missing = sorted(
            set(source_universe_ids) - {universe.universe_id for universe in universes}
        )
        if missing:
            raise ValueError(f"候选池不存在: {', '.join(missing)}")
        markets = {universe.market for universe in universes}
        if len(markets) != 1:
            raise ValueError("只能合并同市场候选池")
        market = next(iter(markets))
        sources: list[JsonDict] = []
        for universe in universes:
            members = self.universes.list_members(universe.universe_id, included_only=True)
            sources.append(
                {
                    "universe_id": universe.universe_id,
                    "source": universe.source,
                    "market": universe.market,
                    "weight": (source_weights or {}).get(universe.universe_id, 1.0),
                    "members": [
                        {
                            "asset_id": member.asset_id,
                            "symbol": member.symbol,
                            "market": member.market,
                            "rank_hint": member.rank_hint,
                        }
                        for member in members
                    ],
                }
            )
        plans = self.merge_service.merge_members(
            target_universe_id=target_universe_id,
            market=market,
            sources=sources,
            as_of=as_of,
        )
        self.universes.upsert_universe(
            universe_id=target_universe_id,
            name=name,
            source="internal:universe_merge",
            market=market,
            strategy_context=strategy_context,
            as_of=as_of,
            total_before_filter=sum(len(source["members"]) for source in sources),
            total_after_filter=len(plans),
            filters={"merge_policy": "weighted_rank_v1"},
            payload={
                "source_universe_ids": source_universe_ids,
                "source_weights": source_weights or {},
            },
        )
        self.universes.replace_members(
            universe_id=target_universe_id,
            members=[plan.to_repository_payload() for plan in plans],
        )
        self.universes.prune_missing_members(
            universe_id=target_universe_id,
            current_asset_ids=[plan.asset_id for plan in plans],
            as_of=as_of,
            removed_reason="not_in_latest_merge",
        )
        return plans

    def rebuild_avoid_pool(
        self,
        *,
        universe_id: str,
        name: str,
        market: str,
        strategy_context: str = "avoid_pool",
        as_of: datetime | None = None,
    ) -> list[UniverseMemberPlan]:
        """根据资产状态和高风险发现重建回避池。"""

        as_of = as_of or datetime.now(tz=UTC)
        assets = self.assets.find_by_market(market, only_tradable=False)
        risks: list[JsonDict] = []
        for asset in assets:
            risks.extend(
                {
                    "asset_id": risk.asset_id,
                    "risk_type": risk.risk_type,
                    "severity": risk.severity,
                    "title": risk.title,
                }
                for risk in self.risks.list_recent_risks(asset_id=asset.asset_id, limit=10)
            )
        plans = self.avoid_policy.build_avoid_members(
            universe_id=universe_id,
            market=market,
            assets=[
                {
                    "asset_id": asset.asset_id,
                    "symbol": asset.symbol,
                    "name": asset.name,
                    "status": asset.status,
                    "tradable": asset.tradable,
                }
                for asset in assets
            ],
            risks=risks,
            as_of=as_of,
        )
        self.universes.upsert_universe(
            universe_id=universe_id,
            name=name,
            source="internal:avoid_pool_policy",
            market=market,
            strategy_context=strategy_context,
            as_of=as_of,
            total_before_filter=len(assets),
            total_after_filter=len(plans),
            filters={"avoid_policy": "data_production_avoid_pool_v1"},
            payload={"risk_count": len(risks)},
        )
        self.universes.replace_members(
            universe_id=universe_id,
            members=[plan.to_repository_payload() for plan in plans],
        )
        return plans
