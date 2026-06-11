"""技术初筛池服务。

技术初筛只基于历史 OHLCV 做轻量粗筛，用于后续数据质量、事件和推荐链路
优先补齐，不代表买入建议。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import sqrt
from statistics import pstdev
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy.orm import Session

from finance_agent.application.asset_eligibility_service import (
    TradeableAssetEligibilityService,
)
from finance_agent.storage.repositories import (
    AssetRepository,
    MarketDataRepository,
    ScreeningRepository,
)

JsonDict = dict[str, Any]

TECHNICAL_SCREENING_SOURCE_TYPE = "technical_screening"
TECHNICAL_SCREENING_STRATEGY = "technical_screening_v1"
TECHNICAL_SCREENING_UNIVERSE_ID = "universe:technical:ashare:main_board"
DEFAULT_TECHNICAL_SCREENING_TTL_DAYS = 3


@dataclass(frozen=True)
class TechnicalScreeningCandidate:
    """单个标的的技术初筛结果。"""

    asset_id: str
    symbol: str
    market: str
    source_type: str
    passed: bool
    technical_score: Decimal
    data_status: str
    liquidity_status: str
    passed_rules: tuple[str, ...]
    failed_rules: tuple[str, ...]
    removed_reason: str | None
    as_of: datetime
    expires_at: datetime
    payload: JsonDict


@dataclass(frozen=True)
class TechnicalScreeningRunResult:
    """一次技术初筛运行摘要。"""

    status: str
    screening_id: str
    universe_id: str
    market: str
    strategy: str
    candidate_count: int
    accepted_count: int
    rejected_count: int
    skipped_count: int
    candidates: tuple[TechnicalScreeningCandidate, ...]
    rule_hits: JsonDict


class TechnicalScreeningService:
    """基于可交易资产池和日 K 数据生成技术初筛池。"""

    def __init__(
        self,
        session: Session | None,
        *,
        assets: AssetRepository | None = None,
        market_data: MarketDataRepository | None = None,
        screenings: ScreeningRepository | None = None,
        eligibility: TradeableAssetEligibilityService | None = None,
    ) -> None:
        self.session = session
        self.assets = assets or (AssetRepository(session) if session is not None else None)
        self.market_data = (
            market_data or (MarketDataRepository(session) if session is not None else None)
        )
        self.screenings = screenings or (
            ScreeningRepository(session) if session is not None else None
        )
        self.eligibility = eligibility or TradeableAssetEligibilityService()

    def screen_ashare(
        self,
        *,
        limit: int = 200,
        timeframe: str = "1d",
        source: str | None = None,
        min_bars: int = 250,
        as_of: datetime | None = None,
        persist: bool = True,
    ) -> TechnicalScreeningRunResult:
        """从数据库读取 A 股可交易资产和最近日 K，生成技术初筛结果。"""

        if self.assets is None or self.market_data is None:
            raise ValueError("screen_ashare 需要可用的数据库会话或注入仓储。")
        assets = self.eligibility.filter_tradeable_assets(
            self.assets.find_by_market("ashare", only_tradable=True)
        )
        if limit > 0:
            assets = assets[:limit]
        bars_by_asset_id = {
            asset.asset_id: self.market_data.list_recent_bars(
                asset_id=asset.asset_id,
                timeframe=timeframe,
                limit=min_bars,
                source=source,
            )
            for asset in assets
        }
        return self.screen_assets(
            assets=assets,
            bars_by_asset_id=bars_by_asset_id,
            as_of=as_of,
            min_bars=min_bars,
            persist=persist,
            timeframe=timeframe,
        )

    def screen_funds(
        self,
        *,
        limit: int = 200,
        timeframe: str = "1d",
        source: str | None = None,
        min_bars: int = 250,
        as_of: datetime | None = None,
        persist: bool = True,
    ) -> TechnicalScreeningRunResult:
        """对场内基金执行同一套 OHLCV 技术粗筛。"""

        if self.assets is None or self.market_data is None:
            raise ValueError("screen_funds 需要可用的数据库会话或注入仓储。")
        assets = self.eligibility.filter_tradeable_assets(
            self.assets.find_by_market("fund", only_tradable=True)
        )
        if limit > 0:
            assets = assets[:limit]
        bars_by_asset_id = {
            asset.asset_id: self.market_data.list_recent_bars(
                asset_id=asset.asset_id,
                timeframe=timeframe,
                limit=min_bars,
                source=source,
            )
            for asset in assets
        }
        return self.screen_assets(
            assets=assets,
            bars_by_asset_id=bars_by_asset_id,
            as_of=as_of,
            min_bars=min_bars,
            persist=persist,
            timeframe=timeframe,
            universe_id="universe:technical:fund:exchange_traded",
            market="fund",
        )

    def screen_assets(
        self,
        *,
        assets: Iterable[Any],
        bars_by_asset_id: Mapping[str, Sequence[Any]],
        as_of: datetime | None = None,
        min_bars: int = 250,
        persist: bool = True,
        timeframe: str = "1d",
        universe_id: str = TECHNICAL_SCREENING_UNIVERSE_ID,
        market: str = "ashare",
        ttl_days: int = DEFAULT_TECHNICAL_SCREENING_TTL_DAYS,
    ) -> TechnicalScreeningRunResult:
        """对已加载资产和 K 线执行技术初筛，便于调度器和测试复用。"""

        evaluated_at = normalize_datetime(as_of or datetime.now(tz=UTC))
        expires_at = evaluated_at + timedelta(days=ttl_days)
        screening_id = build_technical_screening_id(
            market=market,
            as_of=evaluated_at,
        )
        candidates: list[TechnicalScreeningCandidate] = []
        skipped_count = 0
        for asset in assets:
            if not self.eligibility.is_tradeable_asset(asset):
                skipped_count += 1
                continue
            asset_id = str(getattr(asset, "asset_id"))
            bars = list(bars_by_asset_id.get(asset_id, ()))
            candidates.append(
                evaluate_technical_candidate(
                    asset=asset,
                    bars=bars,
                    as_of=evaluated_at,
                    expires_at=expires_at,
                    min_bars=min_bars,
                )
            )

        accepted_count = sum(1 for item in candidates if item.passed)
        rejected_count = len(candidates) - accepted_count
        rule_hits = count_rule_hits(candidates)
        status = "available" if candidates else "unavailable"
        result = TechnicalScreeningRunResult(
            status=status,
            screening_id=screening_id,
            universe_id=universe_id,
            market=market,
            strategy=TECHNICAL_SCREENING_STRATEGY,
            candidate_count=len(candidates),
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            skipped_count=skipped_count,
            candidates=tuple(candidates),
            rule_hits=rule_hits,
        )
        if persist and self.screenings is not None:
            self._persist_result(
                result=result,
                as_of=evaluated_at,
                timeframe=timeframe,
                min_bars=min_bars,
            )
        return result

    def _persist_result(
        self,
        *,
        result: TechnicalScreeningRunResult,
        as_of: datetime,
        timeframe: str,
        min_bars: int,
    ) -> None:
        """写入 screening_results / screening_result_items。"""

        if self.screenings is None:
            return
        self.screenings.upsert_screening_result(
            screening_id=result.screening_id,
            universe_id=result.universe_id,
            strategy=result.strategy,
            market=result.market,
            passed_count=result.accepted_count,
            removed_count=result.rejected_count,
            rules={
                "rule_version": TECHNICAL_SCREENING_STRATEGY,
                "timeframe": timeframe,
                "min_bars": min_bars,
                "ttl_days": DEFAULT_TECHNICAL_SCREENING_TTL_DAYS,
                "hard_rules": [
                    "history_coverage",
                    "trend_ma60",
                    "momentum_20d",
                    "momentum_60d",
                    "drawdown_limit",
                    "volatility_limit",
                    "liquidity_amount",
                ],
            },
            status=result.status,
            as_of=as_of,
            payload={
                "schema_version": "1.0",
                "source_type": TECHNICAL_SCREENING_SOURCE_TYPE,
                "accepted_count": result.accepted_count,
                "rejected_count": result.rejected_count,
                "skipped_count": result.skipped_count,
                "rule_hits": result.rule_hits,
                "recommendation_semantics": "not_buy_signal",
            },
        )
        for candidate in result.candidates:
            self._upsert_candidate(result=result, candidate=candidate)

    def _upsert_candidate(
        self,
        *,
        result: TechnicalScreeningRunResult,
        candidate: TechnicalScreeningCandidate,
    ) -> None:
        payload = dict(candidate.payload)
        payload["source_type"] = TECHNICAL_SCREENING_SOURCE_TYPE
        kwargs = {
            "screening_item_id": f"{result.screening_id}:{candidate.asset_id}",
            "screening_id": result.screening_id,
            "universe_id": result.universe_id,
            "asset_id": candidate.asset_id,
            "symbol": candidate.symbol,
            "market": candidate.market,
            "passed": candidate.passed,
            "removed_reason": candidate.removed_reason,
            "failed_rules": list(candidate.failed_rules),
            "passed_rules": list(candidate.passed_rules),
            "data_status": candidate.data_status,
            "liquidity_status": candidate.liquidity_status,
            "as_of": candidate.as_of,
            "payload": payload,
            "source_type": TECHNICAL_SCREENING_SOURCE_TYPE,
        }
        try:
            self.screenings.upsert_screening_item(**kwargs)
        except TypeError:
            kwargs.pop("source_type", None)
            self.screenings.upsert_screening_item(**kwargs)


def evaluate_technical_candidate(
    *,
    asset: Any,
    bars: Sequence[Any],
    as_of: datetime,
    expires_at: datetime,
    min_bars: int,
) -> TechnicalScreeningCandidate:
    """对单个资产计算技术粗筛结果。"""

    asset_id = str(getattr(asset, "asset_id"))
    symbol = str(getattr(asset, "symbol"))
    market = str(getattr(asset, "market", "ashare") or "ashare")
    normalized_bars = sorted(bars, key=lambda item: getattr(item, "timestamp", as_of))
    if len(normalized_bars) < min_bars:
        return build_candidate(
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            passed=False,
            technical_score=Decimal("0"),
            data_status="insufficient_history",
            liquidity_status="unknown",
            passed_rules=(),
            failed_rules=("history_coverage",),
            removed_reason="历史日 K 覆盖不足",
            as_of=as_of,
            expires_at=expires_at,
            metrics={"bar_count": len(normalized_bars), "min_bars": min_bars},
        )

    metrics = calculate_metrics(normalized_bars)
    passed_rules: list[str] = ["history_coverage"]
    failed_rules: list[str] = []
    score = Decimal("20")
    checks = [
        ("trend_ma60", metrics["close"] > metrics["ma60"], Decimal("15")),
        ("trend_ma120", metrics["close"] > metrics["ma120"], Decimal("10")),
        ("momentum_20d", metrics["return_20d"] > 0, Decimal("12")),
        ("momentum_60d", metrics["return_60d"] > 0, Decimal("12")),
        ("drawdown_limit", metrics["max_drawdown_120d"] >= -0.25, Decimal("12")),
        ("volatility_limit", metrics["volatility_20d"] <= 0.60, Decimal("9")),
        ("liquidity_amount", metrics["amount_avg_20d"] >= 50_000_000, Decimal("10")),
    ]
    for rule, passed, weight in checks:
        if passed:
            passed_rules.append(rule)
            score += weight
        else:
            failed_rules.append(rule)

    passed = not failed_rules and score >= Decimal("75")
    return build_candidate(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        passed=passed,
        technical_score=min(score, Decimal("100")),
        data_status="available",
        liquidity_status="available" if "liquidity_amount" in passed_rules else "weak",
        passed_rules=tuple(passed_rules),
        failed_rules=tuple(failed_rules),
        removed_reason=None if passed else "；".join(failed_rules),
        as_of=as_of,
        expires_at=expires_at,
        metrics=metrics,
    )


def calculate_metrics(bars: Sequence[Any]) -> JsonDict:
    """从最近 K 线计算轻量技术指标。"""

    closes = [float(decimal_attr(item, "close")) for item in bars]
    highs = [float(decimal_attr(item, "high")) for item in bars]
    lows = [float(decimal_attr(item, "low")) for item in bars]
    amounts = [float(decimal_attr(item, "amount") or Decimal("0")) for item in bars]
    returns = [
        (closes[index] / closes[index - 1] - 1.0)
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    latest_close = closes[-1]
    metrics = {
        "bar_count": len(bars),
        "close": latest_close,
        "ma20": mean(closes[-20:]),
        "ma60": mean(closes[-60:]),
        "ma120": mean(closes[-120:]),
        "return_20d": latest_close / closes[-21] - 1.0 if closes[-21] > 0 else 0.0,
        "return_60d": latest_close / closes[-61] - 1.0 if closes[-61] > 0 else 0.0,
        "max_drawdown_120d": max_drawdown(closes[-120:]),
        "volatility_20d": (pstdev(returns[-20:]) * sqrt(252)) if len(returns) >= 20 else 0.0,
        "amount_avg_20d": mean(amounts[-20:]),
        "atr_14": mean(
            [highs[index] - lows[index] for index in range(max(0, len(highs) - 14), len(highs))]
        ),
    }
    return metrics


def build_candidate(
    *,
    asset_id: str,
    symbol: str,
    market: str,
    passed: bool,
    technical_score: Decimal,
    data_status: str,
    liquidity_status: str,
    passed_rules: tuple[str, ...],
    failed_rules: tuple[str, ...],
    removed_reason: str | None,
    as_of: datetime,
    expires_at: datetime,
    metrics: JsonDict,
) -> TechnicalScreeningCandidate:
    return TechnicalScreeningCandidate(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        source_type=TECHNICAL_SCREENING_SOURCE_TYPE,
        passed=passed,
        technical_score=technical_score,
        data_status=data_status,
        liquidity_status=liquidity_status,
        passed_rules=passed_rules,
        failed_rules=failed_rules,
        removed_reason=removed_reason,
        as_of=as_of,
        expires_at=expires_at,
        payload={
            "strategy": TECHNICAL_SCREENING_STRATEGY,
            "source_type": TECHNICAL_SCREENING_SOURCE_TYPE,
            "technical_score": str(technical_score),
            "expires_at": expires_at.isoformat(),
            "recommendation_semantics": "not_buy_signal",
            "metrics": metrics,
        },
    )


def count_rule_hits(candidates: Sequence[TechnicalScreeningCandidate]) -> JsonDict:
    """统计规则命中分布，供任务进度和前端摘要展示。"""

    hits: dict[str, int] = {}
    failures: dict[str, int] = {}
    for candidate in candidates:
        for rule in candidate.passed_rules:
            hits[rule] = hits.get(rule, 0) + 1
        for rule in candidate.failed_rules:
            failures[rule] = failures.get(rule, 0) + 1
    return {"passed_rules": hits, "failed_rules": failures}


def build_technical_screening_id(*, market: str, as_of: datetime) -> str:
    """生成技术初筛运行 ID。"""

    normalized = normalize_datetime(as_of).strftime("%Y%m%dT%H%M%SZ")
    if market == "ashare":
        return f"screen:technical:ashare:main_board:{normalized}"
    return f"screen:technical:{market}:{normalized}"


def normalize_datetime(value: datetime) -> datetime:
    """统一为 UTC aware datetime。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def decimal_attr(item: Any, name: str) -> Decimal:
    """读取 K 线数值字段。"""

    value = getattr(item, name, None)
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def mean(values: Sequence[float]) -> float:
    """计算均值，空序列返回 0。"""

    return sum(values) / len(values) if values else 0.0


def max_drawdown(closes: Sequence[float]) -> float:
    """计算窗口最大回撤，返回负数。"""

    peak = closes[0] if closes else 0.0
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak <= 0:
            continue
        worst = min(worst, close / peak - 1.0)
    return worst
