"""中文金融决策报告模板。

报告生成层只整理 Workflow 已经产生的结构化事实、圆桌观点、复核状态和模型
路由，不新增没有证据来源的判断。
"""

from __future__ import annotations

from typing import Any


def build_chinese_decision_report(
    *,
    title: str,
    summary: str,
    workflow_type: str,
    asset_symbols: list[str],
    decisions: list[dict[str, Any]],
    roundtable_opinions: list[dict[str, Any]],
    high_risk_reviews: list[dict[str, Any]],
    asset_contexts: dict[str, dict[str, Any]],
    model_routes: list[dict[str, Any]],
    review_model_routes: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成前端和审计都可消费的完整中文报告。"""

    decision_section = build_decision_section(decisions=decisions)
    action_plan = build_action_plan(decisions=decisions, high_risk_reviews=high_risk_reviews)
    key_evidence = build_key_evidence(asset_contexts=asset_contexts)
    roundtable_section = build_roundtable_section(roundtable_opinions=roundtable_opinions)
    risk_rebuttal = build_risk_rebuttal_section(
        roundtable_opinions=roundtable_opinions,
        high_risk_reviews=high_risk_reviews,
    )
    data_quality = build_data_quality_section(
        decisions=decisions,
        asset_contexts=asset_contexts,
    )
    memory_references = build_memory_references(asset_contexts=asset_contexts)
    review_status = build_review_status(high_risk_reviews=high_risk_reviews)
    model_routing = build_model_routing(
        model_routes=model_routes,
        review_model_routes=review_model_routes,
    )
    executive_summary = {
        "text": summary,
        "asset_symbols": asset_symbols,
        "workflow_type": workflow_type,
        "decision_count": len(decisions),
        "requires_review_count": review_status["requires_review_count"],
    }
    report = {
        "title": title,
        "summary": summary,
        "workflow_type": workflow_type,
        "asset_symbols": asset_symbols,
        "executive_summary": executive_summary,
        "decision": decision_section,
        "action_plan": action_plan,
        "key_evidence": key_evidence,
        "roundtable_opinions": roundtable_section,
        "risk_rebuttal": risk_rebuttal,
        "data_quality": data_quality,
        "memory_references": memory_references,
        "review_status": review_status,
        "model_routing": model_routing,
        "disclaimer": "本报告仅基于系统已入库数据生成，用于个人研究与决策辅助，不构成投资建议。",
        "decision_actions": [decision.get("action") for decision in decisions],
        "roundtable_roles": sorted({opinion.get("role", "") for opinion in roundtable_opinions}),
        "requires_review_count": review_status["requires_review_count"],
    }
    report["markdown"] = render_report_markdown(report)
    return report


def build_decision_section(*, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """生成裁决章节。"""

    primary = decisions[0] if decisions else {}
    return {
        "primary_action": primary.get("action") or "no_action",
        "primary_decision_type": primary.get("decision_type"),
        "severity": primary.get("severity") or "unknown",
        "confidence": primary.get("confidence"),
        "summary": primary.get("summary") or "暂无主席裁决。",
        "items": decisions,
    }


def build_action_plan(
    *,
    decisions: list[dict[str, Any]],
    high_risk_reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """生成行动计划。"""

    review_assets = {
        item.get("asset_id")
        for item in high_risk_reviews
        if item.get("requires_review")
    }
    plans: list[dict[str, Any]] = []
    for index, decision in enumerate(decisions, start=1):
        action = decision.get("action") or "watch"
        plans.append(
            {
                "step": index,
                "asset_id": decision.get("asset_id"),
                "action": action,
                "priority": "high" if decision.get("asset_id") in review_assets else "normal",
                "instruction": build_action_instruction(action=action),
                "needs_review_before_execution": decision.get("asset_id") in review_assets,
            }
        )
    if not plans:
        plans.append(
            {
                "step": 1,
                "asset_id": None,
                "action": "wait",
                "priority": "normal",
                "instruction": "等待下一次数据刷新后重新评估。",
                "needs_review_before_execution": False,
            }
        )
    return plans


def build_action_instruction(*, action: str) -> str:
    """把内部动作转换为中文执行口径。"""

    mapping = {
        "swap": "进入换股/换币复核，复核通过后再生成交易草案。",
        "buy": "进入买入复核，确认仓位上限、止损线和失效条件。",
        "sell": "进入卖出复核，确认风险证据和替代标的。",
        "reduce": "进入减仓复核，确认仓位调整比例。",
        "watch": "加入或继续观察池，等待触发条件。",
        "risk_watch": "保持风险观察，暂不扩大暴露。",
        "deep_analysis_support": "继续深度分析，可作为候选池重点跟踪对象。",
        "review_next_day": "纳入次日复盘清单。",
        "keep_watching": "继续观察，不执行交易动作。",
        "avoid": "暂不入池、不交易。",
    }
    return mapping.get(action, "保持观察，等待更多确认信号。")


def build_key_evidence(*, asset_contexts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总证据、指标和评分。"""

    evidence_items: list[dict[str, Any]] = []
    for asset_id, context in asset_contexts.items():
        profile = context.get("profile") or {}
        factor_context = context.get("factor") or {}
        score = factor_context.get("score") or {}
        indicator = factor_context.get("indicator_frame") or {}
        for evidence in factor_context.get("evidence") or []:
            evidence_items.append(
                {
                    "asset_id": asset_id,
                    "symbol": profile.get("symbol") or asset_id,
                    "evidence_id": evidence.get("evidence_id"),
                    "source": evidence.get("source"),
                    "title": evidence.get("title"),
                    "summary": evidence.get("summary"),
                    "score": score.get("total_score"),
                    "indicator_frame_id": indicator.get("indicator_frame_id"),
                }
            )
        backtest = extract_backtest_evidence(context=context, factor_context=factor_context)
        if backtest is not None:
            evidence_items.append(
                build_backtest_evidence_item(
                    asset_id=asset_id,
                    symbol=profile.get("symbol") or asset_id,
                    backtest=backtest,
                )
            )
    return evidence_items


def extract_backtest_evidence(
    *,
    context: dict[str, Any],
    factor_context: dict[str, Any],
) -> dict[str, Any] | None:
    """从资产上下文中提取回测证据。"""

    for candidate in (
        context.get("backtest"),
        context.get("backtest_evidence"),
        factor_context.get("backtest_evidence"),
    ):
        if isinstance(candidate, dict):
            return candidate
    return None


def build_backtest_evidence_item(
    *,
    asset_id: str,
    symbol: str,
    backtest: dict[str, Any],
) -> dict[str, Any]:
    """把回测结果转换成报告证据项。"""

    if backtest.get("status") == "available":
        return {
            "asset_id": asset_id,
            "symbol": symbol,
            "evidence_id": backtest.get("backtest_id"),
            "source": "backtest",
            "title": "策略回测证据",
            "summary": backtest.get("summary") or "同策略回测结果可用，但暂无可读摘要。",
            "score": None,
            "indicator_frame_id": None,
        }
    reason = backtest.get("reason") or "暂无同策略回测证据"
    return {
        "asset_id": asset_id,
        "symbol": symbol,
        "evidence_id": backtest.get("backtest_id"),
        "source": "backtest",
        "title": "暂无回测证据",
        "summary": f"{reason}，报告结论仅基于当前评分、信号和风险事实。",
        "score": None,
        "indicator_frame_id": None,
        "certainty_adjustment": backtest.get("certainty_adjustment") or "lower",
    }


def build_roundtable_section(
    *,
    roundtable_opinions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """整理圆桌观点。"""

    return [
        {
            "role": opinion.get("role"),
            "asset_id": opinion.get("asset_id"),
            "stance": opinion.get("stance"),
            "summary": opinion.get("summary"),
            "confidence": opinion.get("confidence"),
            "key_points": normalize_text_list(opinion.get("key_points")),
            "rebuttals": normalize_text_list(opinion.get("rebuttals")),
            "data_gaps": normalize_text_list(opinion.get("data_gaps")),
            "generated_by": opinion.get("generated_by") or "fallback",
            "model_instance_id": opinion.get("model_instance_id"),
            "tool_calls": opinion.get("tool_calls", []),
            "evidence_ids": opinion.get("evidence_ids", []),
            "source_ids": opinion.get("source_ids", []),
        }
        for opinion in roundtable_opinions
    ]


def build_risk_rebuttal_section(
    *,
    roundtable_opinions: list[dict[str, Any]],
    high_risk_reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成风险反驳章节。"""

    risk_opinions = [
        opinion
        for opinion in roundtable_opinions
        if opinion.get("role") == "risk_rebuttal"
    ]
    return {
        "summary": "；".join(opinion.get("summary", "") for opinion in risk_opinions)
        or "暂无明确反方风险，但仍需观察数据质量和事件变化。",
        "risk_opinions": risk_opinions,
        "high_risk_reviews": high_risk_reviews,
    }


def build_data_quality_section(
    *,
    decisions: list[dict[str, Any]],
    asset_contexts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """生成数据质量章节。"""

    items: list[dict[str, Any]] = []
    for decision in decisions:
        asset_id = decision.get("asset_id")
        context = asset_contexts.get(str(asset_id), {})
        quality_items = ((context.get("signal_risk") or {}).get("data_quality") or [])
        items.append(
            {
                "asset_id": asset_id,
                "decision_quality": decision.get("data_quality_status"),
                "quality_snapshots": quality_items,
            }
        )
    statuses = {item.get("decision_quality") for item in items}
    return {
        "status": "available" if statuses <= {"available", None} else "needs_attention",
        "items": items,
    }


def build_memory_references(
    *,
    asset_contexts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """生成金融记忆引用章节。"""

    memories: list[dict[str, Any]] = []
    for asset_id, context in asset_contexts.items():
        profile = context.get("profile") or {}
        for memory in (context.get("memory") or {}).get("memories") or []:
            memory_value = (
                memory.get
                if isinstance(memory, dict)
                else lambda key, row=memory: getattr(row, key, None)
            )
            memories.append(
                {
                    "asset_id": asset_id,
                    "symbol": profile.get("symbol") or asset_id,
                    "memory_id": memory_value("memory_id"),
                    "memory_type": memory_value("memory_type"),
                    "content": memory_value("content"),
                    "confidence": memory_value("confidence"),
                }
            )
    return memories


def build_review_status(*, high_risk_reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """生成复核状态摘要。"""

    requires_review = [item for item in high_risk_reviews if item.get("requires_review")]
    return {
        "requires_review_count": len(requires_review),
        "status": "requires_model_review" if requires_review else "passed_without_escalation",
        "items": high_risk_reviews,
    }


def build_model_routing(
    *,
    model_routes: list[dict[str, Any]],
    review_model_routes: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成模型路由章节。"""

    primary_model = model_routes[0]["model_key"] if model_routes else "deepseek-v4-pro"
    review_model = review_model_routes[0]["model_key"] if review_model_routes else None
    return {
        "primary_model": primary_model,
        "review_model": review_model,
        "primary_routes": model_routes,
        "review_routes": review_model_routes,
        "policy": "常规分析复用 DeepSeek V4 Pro；高风险买卖、换股/换币和冲突场景升级 GPT-5.5 Pro。",
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    """把结构化报告渲染为中文 Markdown。"""

    lines = [
        f"# {report['title']}",
        "",
        "## 执行摘要",
        str(report["executive_summary"]["text"]),
        "",
        "## 决策结论",
        str(report["decision"]["summary"]),
        "",
        "## 行动计划",
    ]
    lines.extend(
        f"- {plan['instruction']}（动作：{plan['action']}）"
        for plan in report["action_plan"]
    )
    lines.extend(["", "## 关键证据"])
    if report["key_evidence"]:
        lines.extend(
            f"- {item.get('symbol')}: {item.get('title')} - {item.get('summary')}"
            for item in report["key_evidence"]
        )
    else:
        lines.append("- 暂无可引用证据。")
    lines.extend(["", "## 圆桌观点"])
    lines.extend(render_roundtable_markdown_lines(report["roundtable_opinions"]))
    lines.extend(
        [
            "",
            "## 风险反驳",
            str(report["risk_rebuttal"]["summary"]),
            "",
            "## 数据质量",
            f"状态：{report['data_quality']['status']}",
            "",
            "## 模型路由与复核",
            f"常规模型：{report['model_routing']['primary_model']}",
            f"复核模型：{report['model_routing']['review_model'] or '无需升级'}",
            f"复核状态：{report['review_status']['status']}",
            "",
            "## 免责声明",
            str(report["disclaimer"]),
        ]
    )
    return "\n".join(lines)


def render_roundtable_markdown_lines(
    roundtable_opinions: list[dict[str, Any]],
) -> list[str]:
    """渲染圆桌观点 Markdown 行。"""

    if not roundtable_opinions:
        return ["- 暂无圆桌观点。"]
    lines: list[str] = []
    for item in roundtable_opinions:
        generated_by = item.get("generated_by") or "fallback"
        lines.append(
            f"- {item.get('role')}（{generated_by}）: {item.get('summary')}"
        )
        lines.extend(f"  - 要点：{point}" for point in item.get("key_points", []))
        lines.extend(f"  - 反方：{rebuttal}" for rebuttal in item.get("rebuttals", []))
        lines.extend(f"  - 缺口：{gap}" for gap in item.get("data_gaps", []))
    return lines


def normalize_text_list(value: object) -> list[str]:
    """把报告字段安全整理为字符串列表。"""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
