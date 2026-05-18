"""验证完整中文报告模板与模型路由/高风险复核接入。

本脚本只验证本项目内部协议：日常分析路由到 DeepSeek V4 Pro，高风险动作
路由到 GPT-5.5 Pro 复核；当前不真实调用外部模型。
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_agent.agents import FinanceAssistantService
from finance_agent.storage.db import create_session_factory, session_scope

REQUIRED_REPORT_SECTIONS = {
    "title",
    "executive_summary",
    "decision",
    "action_plan",
    "key_evidence",
    "roundtable_opinions",
    "risk_rebuttal",
    "data_quality",
    "memory_references",
    "review_status",
    "model_routing",
    "disclaimer",
    "markdown",
}


def main() -> None:
    """执行完整报告与模型复核冒烟。"""

    seed_roundtable_report_data = load_seed_roundtable_report_data()
    session_factory = create_session_factory()
    owner_id = "owner:smoke_rtmr"
    as_of = datetime(2026, 5, 18, 17, 30, tzinfo=UTC)
    portfolio_id = "pf:smoke:rtmr"
    watchlist_id = "wl:smoke:rtmr"
    run_id = "run:smoke:rtmr:1730"
    candidate_asset_id = "asset:smoke:rtmr:cand"
    weak_asset_id = "asset:smoke:rtmr:weak"
    workflow_run_id = "wf:smoke:rtmr:swap:1730"

    with session_scope(session_factory) as session:
        seed_roundtable_report_data(
            session=session,
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            watchlist_id=watchlist_id,
            run_id=run_id,
            candidate_asset_id=candidate_asset_id,
            weak_asset_id=weak_asset_id,
            as_of=as_of,
        )

        assistant = FinanceAssistantService(session)
        result = assistant.run_workflow(
            workflow_type="swap_decision",
            owner_id=owner_id,
            workflow_run_id=workflow_run_id,
            trigger_type="manual",
            started_at=as_of,
            initial_state={
                "owner_id": owner_id,
                "source_asset_id": weak_asset_id,
                "candidate_asset_id": candidate_asset_id,
                "asset_ids": [weak_asset_id, candidate_asset_id],
                "portfolio_id": portfolio_id,
                "watchlist_id": watchlist_id,
                "recommendation_run_id": run_id,
                "session": session,
            },
        )

        report = result.report or {}
        missing_sections = REQUIRED_REPORT_SECTIONS - set(report)
        if missing_sections:
            raise AssertionError(f"完整中文报告缺少字段: {sorted(missing_sections)}")
        if "## 执行摘要" not in report["markdown"] or "## 风险反驳" not in report["markdown"]:
            raise AssertionError("中文报告 markdown 缺少核心章节。")
        if report["review_status"]["requires_review_count"] < 1:
            raise AssertionError("换股/换币高风险动作必须进入复核。")
        if report["model_routing"]["review_model"] != "gpt-5.5-pro":
            raise AssertionError("高风险复核必须路由到 GPT-5.5 Pro。")
        if report["model_routing"]["primary_model"] != "deepseek-v4-pro":
            raise AssertionError("日常分析必须默认路由到 DeepSeek V4 Pro。")

        review_routes = result.final_state.get("review_model_routes", [])
        if not any(route["model_key"] == "gpt-5.5-pro" for route in review_routes):
            raise AssertionError("final_state 缺少 GPT-5.5 Pro 复核路由。")
        primary_routes = result.final_state.get("model_routes", [])
        if not any(route["model_key"] == "deepseek-v4-pro" for route in primary_routes):
            raise AssertionError("final_state 缺少 DeepSeek V4 Pro 日常分析路由。")

        high_risk_reviews = result.final_state.get("high_risk_reviews", [])
        if not any(
            review.get("requires_review")
            and review.get("model_review", {}).get("review_status") == "requires_model_review"
            for review in high_risk_reviews
        ):
            raise AssertionError("高风险复核项缺少模型复核状态。")

        event_types = {
            event.event_type for event in assistant.langgraph_adapter.list_events(workflow_run_id)
        }
        for required_event_type in {"model_route", "model_review", "high_risk_review"}:
            if required_event_type not in event_types:
                raise AssertionError(f"审计事件缺少 {required_event_type}。")

        print(
            {
                "workflow": "swap_decision",
                "report_sections": sorted(REQUIRED_REPORT_SECTIONS),
                "primary_routes": summarize_routes(primary_routes),
                "review_routes": summarize_routes(review_routes),
                "event_types": sorted(event_types),
            }
        )


def summarize_routes(routes: list[dict[str, Any]]) -> list[dict[str, str | None]]:
    """提取便于冒烟输出查看的模型路由摘要。"""

    return [
        {
            "asset_id": route.get("asset_id"),
            "task": route.get("task"),
            "model_key": route.get("model_key"),
        }
        for route in routes
    ]


def load_seed_roundtable_report_data() -> Any:
    """从相邻 smoke 脚本加载种子数据函数。"""

    module_path = Path(__file__).with_name("smoke_roundtable_report_workflows.py")
    spec = importlib.util.spec_from_file_location(
        "smoke_roundtable_report_workflows",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载圆桌报告 smoke 种子数据。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.seed_roundtable_report_data


if __name__ == "__main__":
    main()
