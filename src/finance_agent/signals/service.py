"""可解释信号生成服务。

SignalService 只把 `factor_frames` 映射成 Agent 可消费的方向、分数和解释信号，
不直接输出最终推荐，也不调用 LLM。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import FactorFrameORM
from finance_agent.storage.repositories import FactorFrameRepository, SignalSnapshotRepository

JsonDict = dict[str, Any]

RULE_VERSION = "signal_v1.0.0"


@dataclass(frozen=True)
class SignalComputationResult:
    """单标的信号计算摘要。"""

    status: str
    signal_id: str | None
    asset_id: str
    symbol: str | None
    market: str | None
    horizon: str
    direction: str | None
    score: float | None
    confidence: float | None
    error_message: str | None = None


class SignalService:
    """把因子快照转换成可解释信号快照。"""

    def __init__(self, session: Session) -> None:
        self.factors = FactorFrameRepository(session)
        self.signals = SignalSnapshotRepository(session)

    def compute_for_asset(
        self,
        *,
        asset_id: str,
        horizon: str = "swing",
        rule_version: str = RULE_VERSION,
    ) -> SignalComputationResult:
        """读取最新因子快照并生成信号。"""

        factor = self.factors.get_latest_factor_frame(asset_id=asset_id, horizon=horizon)
        if factor is None:
            return SignalComputationResult(
                status="unavailable",
                signal_id=None,
                asset_id=asset_id,
                symbol=None,
                market=None,
                horizon=horizon,
                direction=None,
                score=None,
                confidence=None,
                error_message="缺少 factor_frame，无法生成信号",
            )

        signal_groups = build_signal_groups(factor)
        score = weighted_signal_score(signal_groups, market=factor.market)
        confidence = signal_confidence(factor=factor, signal_groups=signal_groups)
        direction = direction_from_score(score, signal_groups)
        status = "available" if factor.status == "available" and confidence >= 0.6 else "partial"
        if score is None:
            status = "unavailable"
            score = 0.0
            confidence = 0.0
            direction = "neutral"

        signal_id = build_signal_id(asset_id=factor.asset_id, horizon=horizon, as_of=factor.as_of)
        saved = self.signals.upsert_signal_snapshot(
            signal_id=signal_id,
            asset_id=factor.asset_id,
            symbol=factor.symbol,
            market=factor.market,
            horizon=horizon,
            direction=direction,
            score=to_decimal(score),
            confidence=to_decimal(confidence),
            rule_version=rule_version,
            status=status,
            as_of=factor.as_of,
            payload={
                "schema_version": "1.0",
                "factor_frame_id": factor.factor_frame_id,
                "signal_groups": signal_groups,
                "inputs": {
                    "missing_groups": factor.missing_groups,
                    "partial_groups": factor.payload.get("partial_groups", []),
                    "source_ids": factor.source_ids,
                },
                "explanation": build_signal_explanation(direction, score, confidence),
            },
        )
        return SignalComputationResult(
            status=saved.status,
            signal_id=saved.signal_id,
            asset_id=saved.asset_id,
            symbol=saved.symbol,
            market=saved.market,
            horizon=saved.horizon,
            direction=saved.direction,
            score=decimal_to_float(saved.score),
            confidence=decimal_to_float(saved.confidence),
        )


def build_signal_groups(factor: FactorFrameORM) -> list[JsonDict]:
    """把因子组转换成信号组。"""

    groups = factor.payload.get("factor_groups") or []
    signal_groups: list[JsonDict] = []
    for group in groups:
        group_name = str(group.get("group"))
        score = as_float(group.get("score"))
        direction = direction_from_score(score, [])
        signal_groups.append(
            {
                "group": group_name,
                "status": group.get("status", "unavailable"),
                "direction": direction,
                "score": score,
                "weight": group_weight(group_name, factor.market),
                "summary": summarize_group(group_name, score, group.get("status")),
                "missing_factors": group.get("missing_factors", []),
                "source_ids": group.get("source_ids", []),
            }
        )
    return signal_groups


def weighted_signal_score(signal_groups: list[JsonDict], *, market: str) -> float | None:
    """按可用信号组权重计算总信号分。"""

    weighted_sum = 0.0
    used_weight = 0.0
    for group in signal_groups:
        score = as_float(group.get("score"))
        if score is None:
            continue
        weight = as_float(group.get("weight")) or group_weight(str(group.get("group")), market)
        weighted_sum += score * weight
        used_weight += weight
    if used_weight == 0:
        return None
    return clamp(weighted_sum / used_weight, 0, 100)


def signal_confidence(*, factor: FactorFrameORM, signal_groups: list[JsonDict]) -> float:
    """计算信号置信度，主要反映数据完整度。"""

    total_groups = len(signal_groups) or 1
    available_groups = sum(1 for group in signal_groups if as_float(group.get("score")) is not None)
    partial_groups = len(factor.payload.get("partial_groups") or [])
    confidence = available_groups / total_groups - partial_groups * 0.04
    return clamp(confidence, 0.05, 1.0)


def direction_from_score(score: float | None, signal_groups: list[JsonDict]) -> str:
    """把分数转换成方向。"""

    if score is None:
        return "neutral"
    directions = {
        group.get("direction")
        for group in signal_groups
        if group.get("direction") in {"bullish", "bearish"}
    }
    if len(directions) > 1:
        return "mixed"
    if score >= 65:
        return "bullish"
    if score <= 45:
        return "bearish"
    return "neutral"


def summarize_group(group: str, score: float | None, status: str | None) -> str:
    """生成短解释，供后续中文报告复用。"""

    if status == "unavailable" or score is None:
        return f"{group} 数据暂不可用，信号置信度需要下调。"
    if score >= 65:
        return f"{group} 对当前标的形成正向支持。"
    if score <= 45:
        return f"{group} 对当前标的形成负向约束。"
    return f"{group} 信号偏中性。"


def build_signal_explanation(direction: str, score: float, confidence: float) -> str:
    """生成信号层中文解释。"""

    return f"透明规则信号为 {direction}，分数 {score:.2f}，置信度 {confidence:.2f}。"


def group_weight(group: str, market: str) -> float:
    """返回信号组权重。"""

    if market.startswith("crypto"):
        weights = {
            "technical": 0.45,
            "derivatives": 0.25,
            "event": 0.15,
            "risk": 0.15,
        }
    else:
        weights = {
            "technical": 0.30,
            "fundamental": 0.25,
            "valuation": 0.15,
            "capital_flow": 0.15,
            "event": 0.10,
            "risk": 0.05,
        }
    return weights.get(group, 0.05)


def build_signal_id(*, asset_id: str, horizon: str, as_of: datetime) -> str:
    """生成稳定信号 ID。"""

    normalized_time = as_of.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"signal:{asset_id}:{horizon}:{normalized_time}"


def as_float(value: Any) -> float | None:
    """安全转浮点。"""

    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def to_decimal(value: float) -> Decimal:
    """浮点转 Decimal。"""

    return Decimal(str(round(value, 6)))


def decimal_to_float(value: Decimal | None) -> float | None:
    """Decimal 转浮点。"""

    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def clamp(value: float, low: float, high: float) -> float:
    """裁剪数值区间。"""

    return max(low, min(high, value))
