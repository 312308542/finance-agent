"""结构方法论证据刷新服务。

本服务负责把已入库日 K 转换为 structural-lite v2 输出，并写入
`indicator_frames`。它只做确定性计算和落库，不参与评分、推荐动作或模型决策。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.indicators.methodology_adapters import IchimokuAdapter, PriceBar
from finance_agent.indicators.structural_methodology_adapters import (
    ENGINE_VERSION,
    StructuralMethodologyAdapter,
    StructuralPriceBar,
    red_lines,
)
from finance_agent.storage.repositories import (
    AssetRepository,
    IndicatorFrameRepository,
    MarketDataRepository,
    UniverseRepository,
)

JsonDict = dict[str, Any]
DEFAULT_STRUCTURAL_ENGINES = ("swings", "smc", "harmonic", "elliott", "ichimoku")
STRUCTURAL_LIBRARY = "structural-lite"
ENGINE_SCHEMA_BY_NAME = {
    "swings": "structural_swings_v2",
    "smc": "smc_lite_v2",
    "harmonic": "harmonic_lite_v2",
    "elliott": "elliott_lite_v2",
    "ichimoku": "ichimoku_v1",
}


class StructuralMethodologyRefreshService:
    """收盘后批量刷新结构方法论证据。"""

    def __init__(
        self,
        session: Session | None,
        *,
        assets: AssetRepository | None = None,
        market_data: MarketDataRepository | None = None,
        indicators: IndicatorFrameRepository | None = None,
        universe_repository: UniverseRepository | None = None,
    ) -> None:
        self.session = session
        self.assets = assets or (AssetRepository(session) if session is not None else None)
        self.market_data = (
            market_data or (MarketDataRepository(session) if session is not None else None)
        )
        self.indicators = indicators or (
            IndicatorFrameRepository(session) if session is not None else None
        )
        self.universes = universe_repository or (
            UniverseRepository(session) if session is not None else None
        )

    def refresh(
        self,
        *,
        market: str = "ashare",
        timeframe: str = "1d",
        engines: Sequence[str] | None = None,
        universe_ids: Sequence[str] | None = None,
        lookback_bars: int = 250,
        limit: int | None = None,
        source: str | None = None,
        swing_window: int = 10,
        harmonic_tolerance: float = 0.12,
        harmonic_max_bars_since_d: int = 10,
        fvg_min_atr_ratio: float = 0.3,
        fvg_include_mitigated: bool = False,
        elliott_confidence_threshold: float = 0.6,
        min_bars_per_wave: int = 3,
    ) -> JsonDict:
        """刷新一批资产的结构方法论输出并写入 `indicator_frames`。"""

        if self.market_data is None or self.indicators is None:
            raise ValueError("结构方法论刷新需要可用的 K 线仓储和指标仓储。")
        normalized_market = str(market).strip()
        normalized_timeframe = str(timeframe).strip() or "1d"
        selected_engines = normalize_engines(engines)
        normalized_lookback = max(int(lookback_bars), 1)
        adapter = StructuralMethodologyAdapter(
            swing_window=int(swing_window),
            harmonic_tolerance=float(harmonic_tolerance),
            harmonic_max_bars_since_d=int(harmonic_max_bars_since_d),
            fvg_min_atr_ratio=float(fvg_min_atr_ratio),
            fvg_include_mitigated=bool(fvg_include_mitigated),
            elliott_confidence_threshold=float(elliott_confidence_threshold),
            min_bars_per_wave=int(min_bars_per_wave),
        )
        assets = self.list_candidate_assets(
            market=normalized_market,
            universe_ids=universe_ids,
            limit=limit,
        )
        written_count = 0
        engine_counts = {engine: 0 for engine in selected_engines}
        status_counts: dict[str, int] = {}
        errors: list[JsonDict] = []
        as_of = datetime.now(tz=UTC)
        for asset in assets:
            asset_id = str(asset.asset_id)
            symbol = str(asset.symbol)
            asset_market = str(getattr(asset, "market", normalized_market) or normalized_market)
            try:
                bars = self.market_data.list_recent_bars(
                    asset_id=asset_id,
                    timeframe=normalized_timeframe,
                    limit=normalized_lookback,
                    source=source,
                )
                price_bars = [to_structural_price_bar(bar) for bar in bars]
                for engine, payload in compute_engine_payloads(
                    adapter=adapter,
                    engines=selected_engines,
                    asset_id=asset_id,
                    symbol=symbol,
                    market=asset_market,
                    timeframe=normalized_timeframe,
                    bars=price_bars,
                    as_of=as_of,
                ):
                    self.persist_payload(
                        asset_id=asset_id,
                        symbol=symbol,
                        market=asset_market,
                        timeframe=normalized_timeframe,
                        payload=payload,
                        as_of=as_of,
                    )
                    written_count += 1
                    engine_counts[engine] += 1
                    status = str(payload.get("status") or "unknown")
                    status_counts[status] = status_counts.get(status, 0) + 1
            except Exception as exc:  # noqa: BLE001 - 批量任务需要单标的隔离失败。
                errors.append(
                    {
                        "asset_id": asset_id,
                        "symbol": symbol,
                        "error": str(exc),
                    }
                )
        return {
            "status": "available" if assets else "unavailable",
            "market": normalized_market,
            "timeframe": normalized_timeframe,
            "asset_count": len(assets),
            "written_count": written_count,
            "engine_counts": engine_counts,
            "status_counts": status_counts,
            "error_count": len(errors),
            "errors": errors[:20],
            "engine_version": ENGINE_VERSION,
        }

    def list_candidate_assets(
        self,
        *,
        market: str,
        universe_ids: Sequence[str] | None,
        limit: int | None,
    ) -> list[Any]:
        """按候选池优先获取资产，候选池为空时回退到市场可交易资产。"""

        candidates: list[Any] = []
        if universe_ids and self.universes is not None:
            for universe_id in universe_ids:
                candidates.extend(self.universes.list_members(str(universe_id), included_only=True))
        if not candidates and self.assets is not None:
            candidates = list(self.assets.find_by_market(market, only_tradable=True))
        deduped = dedupe_assets(candidates, market=market)
        if limit is not None and int(limit) > 0:
            return deduped[: int(limit)]
        return deduped

    def persist_payload(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        payload: JsonDict,
        as_of: datetime,
    ) -> None:
        """把单个结构引擎 payload 转换为指标帧写入参数。"""

        if self.indicators is None:
            raise ValueError("结构方法论刷新需要可用的指标仓储。")
        schema_version = str(payload["schema_version"])
        input_start_at = parse_payload_datetime(payload.get("input_start_at"), fallback=as_of)
        input_end_at = parse_payload_datetime(payload.get("input_end_at"), fallback=as_of)
        indicator_frame_id = build_structural_indicator_frame_id(
            asset_id=asset_id,
            timeframe=timeframe,
            schema_version=schema_version,
            input_end_at=input_end_at,
        )
        self.indicators.upsert_indicator_frame(
            indicator_frame_id=indicator_frame_id,
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            horizon=schema_version,
            library=STRUCTURAL_LIBRARY,
            library_version=ENGINE_VERSION,
            input_start_at=input_start_at,
            input_end_at=input_end_at,
            bar_count=int(payload.get("bar_count") or 0),
            status=str(payload.get("status") or "unknown"),
            as_of=as_of,
            payload=payload,
        )


def normalize_engines(engines: Sequence[str] | None) -> list[str]:
    """解析并校验结构引擎名称。"""

    values = [str(engine).strip() for engine in (engines or DEFAULT_STRUCTURAL_ENGINES)]
    selected = [engine for engine in values if engine]
    invalid = [engine for engine in selected if engine not in ENGINE_SCHEMA_BY_NAME]
    if invalid:
        allowed = ", ".join(DEFAULT_STRUCTURAL_ENGINES)
        raise ValueError(f"不支持的结构方法论引擎：{invalid}，可选值：{allowed}")
    return selected or list(DEFAULT_STRUCTURAL_ENGINES)


def compute_engine_payloads(
    *,
    adapter: StructuralMethodologyAdapter,
    engines: Sequence[str],
    asset_id: str,
    symbol: str,
    market: str,
    timeframe: str,
    bars: list[StructuralPriceBar],
    as_of: datetime,
) -> list[tuple[str, JsonDict]]:
    """按引擎列表计算结构 payload。"""

    if not bars:
        return [
            (
                engine,
                build_empty_payload(
                    schema_version=ENGINE_SCHEMA_BY_NAME[engine],
                    asset_id=asset_id,
                    symbol=symbol,
                    market=market,
                    timeframe=timeframe,
                    as_of=as_of,
                ),
            )
            for engine in engines
        ]
    outputs: list[tuple[str, JsonDict]] = []
    for engine in engines:
        if engine == "swings":
            payload = adapter.compute_swings(
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=bars,
            )
        elif engine == "smc":
            payload = adapter.compute_smc(
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=bars,
            )
        elif engine == "harmonic":
            payload = adapter.compute_harmonic(
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=bars,
            )
        elif engine == "elliott":
            payload = adapter.compute_elliott(
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=bars,
            )
        elif len(bars) < 52:
            payload = build_ichimoku_insufficient_payload(
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=bars,
                as_of=as_of,
            )
        else:
            payload = IchimokuAdapter().compute(
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=[
                    PriceBar(
                        timestamp=bar.timestamp,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                    )
                    for bar in bars
                ],
            ).to_indicator_payload()
        outputs.append((engine, payload))
    return outputs


def build_ichimoku_insufficient_payload(
    *,
    asset_id: str,
    symbol: str,
    market: str,
    timeframe: str,
    bars: list[StructuralPriceBar],
    as_of: datetime,
) -> JsonDict:
    """构造 Ichimoku 预热不足时的可审计指标帧。"""

    input_start_at = bars[0].timestamp if bars else as_of
    input_end_at = bars[-1].timestamp if bars else as_of
    return {
        "schema_version": "ichimoku_v1",
        "status": "insufficient_data",
        "asset_id": asset_id,
        "symbol": symbol,
        "market": market,
        "timeframe": timeframe,
        "input_start_at": input_start_at.isoformat(),
        "input_end_at": input_end_at.isoformat(),
        "bar_count": len(bars),
        "lines": {},
        "signals": [],
        "evidence_id": build_structural_evidence_id(
            schema_version="ichimoku_v1",
            asset_id=asset_id,
            timeframe=timeframe,
            input_end_at=input_end_at,
        ),
        "caveats": [f"一目均衡计算至少需要 52 根 K 线，当前只有 {len(bars)} 根。"],
        "red_lines": [
            "一目均衡线必须由确定性适配器计算，LLM 只能解读。",
            "不得用模型自行补算缺失线值或修改信号方向。",
        ],
    }


def build_empty_payload(
    *,
    schema_version: str,
    asset_id: str,
    symbol: str,
    market: str,
    timeframe: str,
    as_of: datetime,
) -> JsonDict:
    """构造无 K 线时仍可审计的 insufficient payload。"""

    return {
        "schema_version": schema_version,
        "status": "insufficient_data",
        "asset_id": asset_id,
        "symbol": symbol,
        "market": market,
        "timeframe": timeframe,
        "engine": "finance-agent-structural-lite",
        "engine_version": ENGINE_VERSION,
        "bar_count": 0,
        "as_of_semantics": "confirmed_only",
        "input_start_at": as_of.isoformat(),
        "input_end_at": as_of.isoformat(),
        "data_warnings": {"duplicate_timestamp_count": 0},
        "evidence_id": build_structural_evidence_id(
            schema_version=schema_version,
            asset_id=asset_id,
            timeframe=timeframe,
            input_end_at=as_of,
        ),
        "caveats": ["没有可用 K 线，结构方法论无法计算。"],
        "red_lines": red_lines("结构方法论"),
    }


def to_structural_price_bar(bar: Any) -> StructuralPriceBar:
    """把标准 K 线 ORM 或测试替身转换为结构引擎输入。"""

    return StructuralPriceBar(
        timestamp=parse_payload_datetime(bar.timestamp, fallback=datetime.now(tz=UTC)),
        open=to_float(bar.open),
        high=to_float(bar.high),
        low=to_float(bar.low),
        close=to_float(bar.close),
        volume=to_float(getattr(bar, "volume", 0)),
    )


def to_float(value: Any) -> float:
    """把数据库 Decimal 等数值转换为结构引擎浮点输入。"""

    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def dedupe_assets(candidates: Iterable[Any], *, market: str) -> list[Any]:
    """按 asset_id 去重并过滤市场。"""

    deduped: dict[str, Any] = {}
    for candidate in candidates:
        asset_id = str(getattr(candidate, "asset_id", "")).strip()
        candidate_market = str(getattr(candidate, "market", market) or market)
        if not asset_id or candidate_market != market or asset_id in deduped:
            continue
        deduped[asset_id] = candidate
    return list(deduped.values())


def parse_payload_datetime(value: Any, *, fallback: datetime) -> datetime:
    """解析 payload 中的 ISO 时间。"""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def build_structural_indicator_frame_id(
    *,
    asset_id: str,
    timeframe: str,
    schema_version: str,
    input_end_at: datetime,
) -> str:
    """生成稳定且长度可控的结构指标帧 ID。"""

    digest = hashlib.sha1(
        f"{asset_id}|{timeframe}|{schema_version}|{input_end_at.isoformat()}".encode()
    ).hexdigest()[:16]
    return f"struct:{schema_version}:{digest}"


def build_structural_evidence_id(
    *,
    schema_version: str,
    asset_id: str,
    timeframe: str,
    input_end_at: datetime,
) -> str:
    """生成无 K 线场景下的结构证据 ID。"""

    normalized = input_end_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{schema_version}:{asset_id}:{timeframe}:{normalized}"
