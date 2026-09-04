"""评分策略配置。

本模块只定义策略 payload 的校验、默认种子和评分快照组装。策略的持久化
由仓储层负责，评分服务只消费已经校验过的策略对象。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

JsonDict = dict[str, Any]

VALID_STRATEGY_MARKETS = {"ashare", "crypto_spot", "crypto_future"}
VALID_STRATEGY_STATUSES = {"active", "draft", "archived"}
WEIGHT_SUM_TOLERANCE = 0.001


def default_scoring_strategy_seeds() -> list[JsonDict]:
    """返回内置评分策略种子。"""

    return [
        {
            "strategy_id": "strategy:ashare:adaptive_v1",
            "market": "ashare",
            "name": "A 股自适应 Alpha V1",
            "description": "按市场状态在趋势、结构、板块、资金、基本面和收益风险六组间切换权重。",
            "engine": "adaptive_alpha_v1",
            "group_weights": {
                "trend": 0.25,
                "structure": 0.20,
                "sector_leadership": 0.20,
                "capital_flow": 0.15,
                "fundamental_valuation": 0.10,
                "tradability_return_risk": 0.10,
            },
            "missing_penalty": {"per_missing_group": 5.0, "per_partial_group": 2.0},
            "status": "active",
        },
        {
            "strategy_id": "strategy:ashare:balanced_growth",
            "market": "ashare",
            "name": "A 股均衡成长",
            "description": "延续当前 A 股透明评分权重，兼顾技术、基本面、估值和资金流。",
            "group_weights": {
                "technical": 0.28,
                "fundamental": 0.22,
                "valuation": 0.15,
                "capital_flow": 0.15,
                "liquidity": 0.08,
                "event": 0.07,
                "event_decay": 0.02,
                "risk": 0.03,
            },
            "missing_penalty": {"per_missing_group": 4.0, "per_partial_group": 1.5},
            "status": "active",
        },
        {
            "strategy_id": "strategy:ashare:short_swing",
            "market": "ashare",
            "name": "A 股短线波段",
            "description": "提高技术面和资金流权重，适合短线波段候选排序。",
            "group_weights": {
                "technical": 0.40,
                "capital_flow": 0.22,
                "fundamental": 0.12,
                "valuation": 0.08,
                "liquidity": 0.08,
                "event": 0.06,
                "event_decay": 0.02,
                "risk": 0.02,
            },
            "missing_penalty": {"per_missing_group": 4.0, "per_partial_group": 1.5},
            "status": "active",
        },
        {
            "strategy_id": "strategy:ashare:defensive",
            "market": "ashare",
            "name": "A 股防守质量",
            "description": "提高基本面、估值和风险约束权重，适合偏稳健的观察池排序。",
            "group_weights": {
                "technical": 0.18,
                "fundamental": 0.30,
                "valuation": 0.20,
                "capital_flow": 0.08,
                "liquidity": 0.08,
                "event": 0.06,
                "event_decay": 0.02,
                "risk": 0.08,
            },
            "missing_penalty": {"per_missing_group": 4.0, "per_partial_group": 1.5},
            "status": "active",
        },
        {
            "strategy_id": "strategy:ashare:theme_momentum",
            "market": "ashare",
            "name": "A 股题材动量",
            "description": "偏向板块强度、龙头地位、资金流和事件催化，适合题材/短线画像候选排序。",
            "group_weights": {
                "sector_strength": 0.26,
                "leadership": 0.24,
                "capital_flow": 0.18,
                "technical": 0.14,
                "event": 0.08,
                "liquidity": 0.05,
                "valuation": 0.02,
                "fundamental": 0.01,
                "risk": 0.02,
            },
            "missing_penalty": {"per_missing_group": 3.0, "per_partial_group": 1.5},
            "status": "active",
        },
        {
            "strategy_id": "strategy:ashare:short_theme_mixed_v1",
            "market": "ashare",
            "name": "A 股短线题材混合 V1",
            "description": "固定使用短线波段与题材动量权重的算术平均，不按回测结果调参。",
            "group_weights": {
                "technical": 0.27,
                "capital_flow": 0.20,
                "sector_strength": 0.13,
                "leadership": 0.12,
                "event": 0.07,
                "fundamental": 0.065,
                "liquidity": 0.065,
                "valuation": 0.05,
                "risk": 0.02,
                "event_decay": 0.01,
            },
            "missing_penalty": {"per_missing_group": 3.5, "per_partial_group": 1.5},
            "status": "active",
        },
        {
            "strategy_id": "strategy:crypto:crypto_swing",
            "market": "crypto_spot",
            "name": "数字货币波段",
            "description": "延续当前数字货币透明评分权重，突出技术、衍生品和风险。",
            "group_weights": {
                "technical": 0.38,
                "derivatives": 0.25,
                "liquidity": 0.12,
                "event": 0.10,
                "event_decay": 0.05,
                "risk": 0.10,
            },
            "missing_penalty": {"per_missing_group": 4.0, "per_partial_group": 1.5},
            "status": "active",
        },
    ]


def validate_scoring_strategy_payload(payload: Mapping[str, Any]) -> JsonDict:
    """校验评分策略 payload，并返回可安全持久化的结构。"""

    required = {
        "strategy_id",
        "market",
        "name",
        "description",
        "group_weights",
        "missing_penalty",
        "status",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"评分策略缺少必要字段：{', '.join(missing)}")

    market = str(payload["market"])
    if market not in VALID_STRATEGY_MARKETS:
        raise ValueError(f"评分策略 market 不合法：{market}")

    status = str(payload["status"])
    if status not in VALID_STRATEGY_STATUSES:
        raise ValueError(f"评分策略 status 不合法：{status}")

    group_weights = normalize_numeric_mapping(payload["group_weights"], field_name="group_weights")
    weight_sum = sum(group_weights.values())
    if abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ValueError(f"评分策略权重和必须接近 1，当前为 {weight_sum:.6f}")

    missing_penalty = normalize_missing_penalty(payload["missing_penalty"])
    return {
        "strategy_id": str(payload["strategy_id"]),
        "market": market,
        "name": str(payload["name"]),
        "description": str(payload["description"]),
        "group_weights": group_weights,
        "missing_penalty": missing_penalty,
        "status": status,
    }


def strategy_weight_snapshot(strategy: Any) -> JsonDict:
    """生成评分入库使用的策略权重快照。"""

    return {
        "strategy_id": str(strategy.strategy_id),
        "group_weights": normalize_numeric_mapping(
            strategy.group_weights,
            field_name="group_weights",
        ),
        "missing_penalty": normalize_missing_penalty(strategy.missing_penalty),
    }


def normalize_numeric_mapping(value: Any, *, field_name: str) -> dict[str, float]:
    """把 JSON 数值映射统一转为 `dict[str, float]`。"""

    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{field_name} 必须是非空对象。")
    parsed: dict[str, float] = {}
    for key, raw in value.items():
        number = float(raw)
        if number < 0:
            raise ValueError(f"{field_name}.{key} 不能为负数。")
        parsed[str(key)] = number
    return parsed


def normalize_missing_penalty(value: Any) -> dict[str, float]:
    """校验缺失惩罚配置。"""

    parsed = normalize_numeric_mapping(value, field_name="missing_penalty")
    return {
        "per_missing_group": float(parsed.get("per_missing_group", 4.0)),
        "per_partial_group": float(parsed.get("per_partial_group", 1.5)),
    }
