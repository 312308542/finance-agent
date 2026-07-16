"""用真实大盘环境样本验证推荐买入闸门是否生效。

脚本只读数据库：读取最新筛选/评分/信号/风险，分别套入弱市、震荡市、
强市样本生成的 market_regime 上下文，复用推荐裁决函数计算动作分布。
它用于方案 19/10 的真实验收，不写入 recommendation_runs。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from finance_agent.recommendations.service import (
    RecommendationDecisionContext,
    decide_action,
)
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import (
    AssetScoreORM,
    RiskFindingORM,
    ScreeningResultORM,
    SignalSnapshotORM,
)
from finance_agent.storage.repositories import (
    AssetScoreRepository,
    RiskRepository,
    ScreeningRepository,
    SignalSnapshotRepository,
)
from scripts.data.build_market_regime_samples import build_report_from_database, parse_date

JsonDict = dict[str, Any]


def main() -> None:
    """执行真实库只读验收并输出 JSON 或 Markdown。"""

    args = parse_args()
    profile_style_tendency = parse_profile_style_tendency(args.profile_style_tendency)
    result = validate_market_regime_gate_from_database(
        market=args.market,
        strategy=args.strategy,
        horizon=args.horizon,
        score_limit=args.score_limit,
        index_asset_id=args.index_asset_id,
        start_date=parse_date(args.start_date) if args.start_date else None,
        end_date=parse_date(args.end_date) if args.end_date else None,
        sample_limit=args.sample_limit,
        profile_style_tendency=profile_style_tendency,
    )
    if args.format == "markdown":
        print(render_markdown_validation(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="验证大盘环境闸门是否收紧推荐买入")
    parser.add_argument("--market", default="ashare", help="市场，默认 ashare")
    parser.add_argument("--strategy", default="balanced_swing_v1", help="筛选/评分策略")
    parser.add_argument("--horizon", default="swing", help="推荐周期")
    parser.add_argument("--score-limit", type=int, default=200, help="参与验收的候选数量")
    parser.add_argument("--index-asset-id", default="ashare:index:000001", help="大盘指数资产 ID")
    parser.add_argument("--start-date", default=None, help="样本起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="样本结束日期 YYYY-MM-DD")
    parser.add_argument("--sample-limit", type=int, default=1, help="每类环境使用的样本数量")
    parser.add_argument(
        "--profile-style-tendency",
        default='{"theme":0.7,"timing_posture":"balanced"}',
        help="用户画像风格 JSON，用于验证按画像调节闸门",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json", help="输出格式")
    return parser.parse_args()


def validate_market_regime_gate_from_database(
    *,
    market: str,
    strategy: str,
    horizon: str,
    score_limit: int,
    index_asset_id: str,
    start_date: date | None,
    end_date: date | None,
    sample_limit: int,
    profile_style_tendency: JsonDict,
) -> JsonDict:
    """从真实数据库读取样本和候选，执行大盘环境闸门验收。"""

    sample_report = build_report_from_database(
        index_asset_id=index_asset_id,
        start_date=start_date,
        end_date=end_date,
        market=market,
        timeframe="1d",
        limit=max(sample_limit, 1),
    )
    sample_payloads = pick_regime_samples(sample_report)

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        screening, scores, signals_by_asset, risks_by_asset = load_latest_screening_context(
            session=session,
            market=market,
            strategy=strategy,
            horizon=horizon,
            score_limit=score_limit,
        )
        regime_summaries = [
            evaluate_actions_under_regime(
                scores=scores,
                signals_by_asset=signals_by_asset,
                risks_by_asset=risks_by_asset,
                market_regime=sample,
                profile_style_tendency=profile_style_tendency,
            )
            for sample in sample_payloads
        ]

    gate_summary = summarize_gate_validation(regime_summaries=regime_summaries)
    return {
        "passed": gate_summary["passed"],
        "checks": gate_summary["checks"],
        "screening": {
            "screening_id": screening.screening_id,
            "universe_id": screening.universe_id,
            "strategy": screening.strategy,
            "market": screening.market,
            "as_of": screening.as_of.isoformat(),
            "score_limit": score_limit,
            "score_count": len(scores),
        },
        "sample_source": {
            "index_asset_id": sample_report.get("index_asset_id"),
            "index_source": sample_report.get("index_source"),
            "date_range": sample_report.get("date_range"),
        },
        "profile_style_tendency": profile_style_tendency,
        "regime_summaries": regime_summaries,
        "action_delta": gate_summary["action_delta"],
    }


def load_latest_screening_context(
    *,
    session: Any,
    market: str,
    strategy: str,
    horizon: str,
    score_limit: int,
) -> tuple[
    ScreeningResultORM,
    list[AssetScoreORM],
    dict[str, SignalSnapshotORM | None],
    dict[str, list[RiskFindingORM]],
]:
    """读取最新筛选结果及其裁决所需上下文。"""

    screening = ScreeningRepository(session).get_latest_screening_result(
        market=market,
        strategy=strategy,
    )
    if screening is None:
        raise RuntimeError(f"未找到市场 {market}、策略 {strategy} 的筛选结果。")

    scores = AssetScoreRepository(session).list_scores_for_screening(screening.screening_id)[:score_limit]
    if not scores:
        raise RuntimeError(f"筛选结果 {screening.screening_id} 没有关联评分。")

    signal_repository = SignalSnapshotRepository(session)
    risk_repository = RiskRepository(session)
    signals_by_asset = {
        score.asset_id: signal_repository.get_latest_signal(asset_id=score.asset_id, horizon=horizon)
        for score in scores
    }
    risks_by_asset = {
        score.asset_id: risk_repository.list_recent_risks(asset_id=score.asset_id, limit=10)
        for score in scores
    }
    return screening, scores, signals_by_asset, risks_by_asset


def pick_regime_samples(sample_report: Mapping[str, Any]) -> list[JsonDict]:
    """从样本报告中各取一个弱市、震荡市、强市样本。"""

    samples = sample_report.get("samples") or {}
    picked: list[JsonDict] = []
    for regime in ("bear", "range", "bull"):
        items = list(samples.get(regime) or [])
        if not items:
            raise RuntimeError(f"样本报告缺少 {regime} 环境，无法执行 D-007 验收。")
        picked.append(market_regime_payload(items[0]))
    return picked


def market_regime_payload(sample: Mapping[str, Any]) -> JsonDict:
    """把样本报告中的单日样本转换为推荐裁决上下文。"""

    return {
        "as_of": sample.get("as_of"),
        "regime": sample.get("regime"),
        "strength": sample.get("strength"),
        "risk_multiplier": sample.get("risk_multiplier"),
        "index_trend_20d": sample.get("index_trend_20d"),
        "index_trend_60d": sample.get("index_trend_60d"),
        "volatility_20d": sample.get("volatility_20d"),
        "advance_decline_ratio": sample.get("advance_decline_ratio"),
        "limit_up_down_ratio": sample.get("limit_up_down_ratio"),
        "evidence_ids": list(sample.get("evidence_ids") or []),
        "reasons": list(sample.get("reasons") or []),
    }


def evaluate_actions_under_regime(
    *,
    scores: Sequence[Any],
    signals_by_asset: Mapping[str, Any | None],
    risks_by_asset: Mapping[str, Sequence[Any]],
    market_regime: Mapping[str, Any],
    profile_style_tendency: JsonDict,
) -> JsonDict:
    """用指定大盘环境对同一批候选重新计算动作分布。"""

    total = len(scores)
    counts: Counter[str] = Counter()
    examples: list[JsonDict] = []
    threshold_context = RecommendationDecisionContext(
        rank=1,
        total=total,
        style_tendency=profile_style_tendency,
        market_regime=dict(market_regime),
    )
    for rank, score in enumerate(scores, start=1):
        context = RecommendationDecisionContext(
            rank=rank,
            total=total,
            style_tendency=profile_style_tendency,
            market_regime=dict(market_regime),
        )
        action = decide_action(
            score=score,
            signal=signals_by_asset.get(score.asset_id),
            risks=list(risks_by_asset.get(score.asset_id) or []),
            decision_context=context,
        )
        counts[action] += 1
        if action == "buy_candidate" and len(examples) < 5:
            examples.append(
                {
                    "rank": rank,
                    "asset_id": score.asset_id,
                    "symbol": score.symbol,
                    "total_score": float(score.total_score),
                    "percentile": round(context.percentile, 6),
                }
            )

    normalized_counts = {
        "buy_candidate": counts.get("buy_candidate", 0),
        "watch": counts.get("watch", 0),
        "avoid": counts.get("avoid", 0),
    }
    return {
        "regime": market_regime.get("regime"),
        "strength": market_regime.get("strength"),
        "as_of": market_regime.get("as_of"),
        "risk_multiplier": market_regime.get("risk_multiplier"),
        "candidate_count": total,
        "action_counts": normalized_counts,
        "buy_percentile_threshold": threshold_context.buy_percentile_threshold,
        "adjusted_buy_percentile_threshold": threshold_context.adjusted_buy_percentile_threshold,
        "buy_examples": examples,
        "evidence_ids": list(market_regime.get("evidence_ids") or []),
    }


def summarize_gate_validation(*, regime_summaries: Sequence[Mapping[str, Any]]) -> JsonDict:
    """汇总弱市闸门验收结论。"""

    by_regime = {str(item.get("regime")): item for item in regime_summaries}
    bear = by_regime.get("bear")
    range_market = by_regime.get("range")
    checks: JsonDict = {"has_bear_and_range": bear is not None and range_market is not None}
    action_delta: JsonDict = {}
    if bear is not None and range_market is not None:
        bear_buy = int((bear.get("action_counts") or {}).get("buy_candidate") or 0)
        range_buy = int((range_market.get("action_counts") or {}).get("buy_candidate") or 0)
        action_delta["bear_vs_range_buy_candidate"] = bear_buy - range_buy
        checks["bear_buy_not_above_range"] = bear_buy <= range_buy
        if (
            bear.get("adjusted_buy_percentile_threshold") is not None
            and range_market.get("adjusted_buy_percentile_threshold") is not None
        ):
            checks["bear_threshold_below_range"] = float(
                bear["adjusted_buy_percentile_threshold"]
            ) < float(range_market["adjusted_buy_percentile_threshold"])

    return {
        "passed": all(bool(value) for value in checks.values()),
        "checks": checks,
        "action_delta": action_delta,
    }


def parse_profile_style_tendency(value: str) -> JsonDict:
    """解析用户画像风格 JSON。"""

    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("--profile-style-tendency 必须是合法 JSON。") from exc
    if not isinstance(payload, dict):
        raise ValueError("--profile-style-tendency 必须是 JSON 对象。")
    return payload


def render_markdown_validation(result: Mapping[str, Any]) -> str:
    """渲染 Markdown 验收报告。"""

    screening = result.get("screening") or {}
    lines = [
        "# 大盘环境闸门真实样本验收",
        "",
        f"- 验收结论：{'通过' if result.get('passed') else '未通过'}",
        f"- 筛选批次：`{screening.get('screening_id')}`",
        f"- 候选数量：{screening.get('score_count')}",
        f"- 用户画像：`{json.dumps(result.get('profile_style_tendency') or {}, ensure_ascii=False)}`",
        "",
        "| 环境 | 日期 | 动作 buy/watch/avoid | 基础阈值 | 调整后阈值 |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for item in result.get("regime_summaries") or []:
        counts = item.get("action_counts") or {}
        lines.append(
            "| {regime} | {as_of} | {buy}/{watch}/{avoid} | {base:.2%} | {adjusted:.2%} |".format(
                regime=item.get("regime"),
                as_of=item.get("as_of"),
                buy=int(counts.get("buy_candidate") or 0),
                watch=int(counts.get("watch") or 0),
                avoid=int(counts.get("avoid") or 0),
                base=float(item.get("buy_percentile_threshold") or 0),
                adjusted=float(item.get("adjusted_buy_percentile_threshold") or 0),
            )
        )
    lines.extend(
        [
            "",
            "## 检查项",
            "",
        ]
    )
    for key, value in (result.get("checks") or {}).items():
        lines.append(f"- {key}: {'通过' if value else '未通过'}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
