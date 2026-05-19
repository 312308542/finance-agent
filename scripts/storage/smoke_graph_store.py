"""验证知识图谱 GraphStore 配置、同步、CLI 和工具入口。

它不要求本机真实 Neo4j / Apache AGE 可用，只验证 dry-run 后端选择、图谱变更
构建、五类图谱查询、CLI graph 命令和 MCP 工具注册协议。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.agents.tools import FinanceToolRuntime
from finance_agent.graph import GraphSyncService, load_graph_store_settings
from finance_agent.graph.stores import DryRunGraphStore
from finance_agent.mcp_server.server import create_mcp_server
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    AssetRepository,
    DecisionLogRepository,
    EventRepository,
    MemoryRepository,
    RiskRepository,
    WatchlistRepository,
)


def main() -> None:
    """执行 GraphStore 冒烟。"""

    verify_config_file_backend_selection()

    os.environ["FINANCE_AGENT_GRAPH_BACKEND"] = "neo4j"
    os.environ["FINANCE_AGENT_GRAPH_ENABLED"] = "true"
    os.environ["FINANCE_AGENT_GRAPH_DRY_RUN"] = "true"

    settings = load_graph_store_settings()
    if settings.backend != "neo4j":
        raise AssertionError("默认 smoke 配置必须选择 neo4j 后端。")
    if not settings.dry_run:
        raise AssertionError("smoke 不应连接真实图数据库。")

    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M%S")
    owner_id = "owner:graph_smoke"
    asset_id = f"asset:graph_smoke:{stamp}"
    similar_asset_id = f"asset:graph_smoke_similar:{stamp}"
    decision_id = f"decision:graph_smoke:{stamp}"
    similar_decision_id = f"decision:graph_smoke_similar:{stamp}"
    memory_id = f"memory:graph_smoke:{stamp}"
    negative_memory_id = f"memory:graph_smoke_negative:{stamp}"
    similar_memory_id = f"memory:graph_smoke_similar:{stamp}"
    event_id = f"watch_event:graph_smoke:{stamp}"
    similar_event_id = f"watch_event:graph_smoke_similar:{stamp}"
    evidence_id = f"evidence:graph_smoke:{stamp}"
    similar_evidence_id = f"evidence:graph_smoke_similar:{stamp}"
    risk_id = f"risk:graph_smoke:{stamp}"
    similar_risk_id = f"risk:graph_smoke_similar:{stamp}"
    review_task_id = f"review:graph_smoke:{stamp}"
    now = datetime.now(ZoneInfo("Asia/Shanghai"))

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        asset_repo = AssetRepository(session)
        asset_repo.upsert_asset(
            asset_id=asset_id,
            market="ashare",
            symbol=f"GS{stamp[-6:]}",
            name="图谱冒烟标的",
            asset_type="stock",
            exchange="SSE",
            currency="CNY",
            tradable=True,
            status="active",
            payload={"source": "smoke_graph_store"},
        )
        asset_repo.upsert_asset(
            asset_id=similar_asset_id,
            market="ashare",
            symbol=f"GX{stamp[-6:]}",
            name="图谱冒烟相似标的",
            asset_type="stock",
            exchange="SZSE",
            currency="CNY",
            tradable=True,
            status="active",
            payload={"source": "smoke_graph_store"},
        )
        event_repo = EventRepository(session)
        event_repo.upsert_evidence(
            evidence_id=evidence_id,
            evidence_type="factor_snapshot",
            asset_id=asset_id,
            source="smoke_graph_store",
            title="趋势和资金证据",
            summary="趋势向上，资金改善。",
            reliability="high",
            as_of=now,
            collected_at=now,
            payload={"source": "smoke_graph_store"},
        )
        event_repo.upsert_evidence(
            evidence_id=similar_evidence_id,
            evidence_type="factor_snapshot",
            asset_id=similar_asset_id,
            source="smoke_graph_store",
            title="相似趋势证据",
            summary="历史上出现过相似趋势和风险组合。",
            reliability="high",
            as_of=now,
            collected_at=now,
            payload={"source": "smoke_graph_store"},
        )
        RiskRepository(session).upsert_risk_finding(
            risk_id=risk_id,
            scope="asset",
            risk_type="crowded_trade",
            severity="high",
            title="短线拥挤风险",
            as_of=now,
            asset_id=asset_id,
            score=Decimal("0.720000"),
            description="量价快速放大，存在短线拥挤。",
            evidence_ids=[evidence_id],
            payload={"source": "smoke_graph_store"},
        )
        RiskRepository(session).upsert_risk_finding(
            risk_id=similar_risk_id,
            scope="asset",
            risk_type="crowded_trade",
            severity="high",
            title="历史拥挤风险",
            as_of=now,
            asset_id=similar_asset_id,
            score=Decimal("0.700000"),
            description="历史标的也出现短线拥挤。",
            evidence_ids=[similar_evidence_id],
            payload={"source": "smoke_graph_store"},
        )
        DecisionLogRepository(session).insert_decision_log(
            decision_id=decision_id,
            owner_id=owner_id,
            asset_id=asset_id,
            decision_type="watchlist_intake",
            suggested_action="watch",
            user_action="pending",
            summary="图谱冒烟：建议加入观察池。",
            reason_ids=[memory_id],
            risk_ids=[risk_id],
            evidence_ids=[evidence_id],
            created_at=now,
            payload={"source": "smoke_graph_store"},
        )
        DecisionLogRepository(session).insert_decision_log(
            decision_id=similar_decision_id,
            owner_id=owner_id,
            asset_id=similar_asset_id,
            decision_type="watchlist_intake",
            suggested_action="watch",
            user_action="confirmed",
            summary="图谱冒烟：历史相似标的也曾加入观察池。",
            reason_ids=[similar_memory_id],
            risk_ids=[similar_risk_id],
            evidence_ids=[similar_evidence_id],
            created_at=now - timedelta(days=7),
            payload={"source": "smoke_graph_store"},
        )
        memory_repo = MemoryRepository(session)
        memory_repo.upsert_memory(
            memory_id=memory_id,
            owner_id=owner_id,
            memory_type="candidate_intake_reason",
            scope="asset",
            asset_id=asset_id,
            source_decision_id=decision_id,
            content="图谱冒烟：资金和趋势条件满足，纳入观察池。",
            confidence=Decimal("0.900000"),
            status="active",
            payload={"source": "smoke_graph_store"},
        )
        memory_repo.upsert_memory(
            memory_id=negative_memory_id,
            owner_id=owner_id,
            memory_type="risk_invalid_reason",
            scope="asset",
            asset_id=asset_id,
            source_decision_id=decision_id,
            content="图谱冒烟：若短线拥挤继续升温，需要剔除或卖出。",
            confidence=Decimal("0.800000"),
            status="active",
            payload={"source": "smoke_graph_store"},
        )
        memory_repo.upsert_memory(
            memory_id=similar_memory_id,
            owner_id=owner_id,
            memory_type="candidate_intake_reason",
            scope="asset",
            asset_id=similar_asset_id,
            source_decision_id=similar_decision_id,
            content="图谱冒烟：历史相似标的曾因趋势和资金改善纳入观察池。",
            confidence=Decimal("0.850000"),
            status="active",
            payload={"source": "smoke_graph_store"},
        )
        memory_repo.upsert_review_task(
            review_task_id=review_task_id,
            owner_id=owner_id,
            asset_id=asset_id,
            source_decision_id=decision_id,
            review_type="candidate_follow_up",
            due_at=now + timedelta(days=1),
            status="pending",
            review_questions=[{"question": "入池理由是否仍成立？"}],
            payload={"source": "smoke_graph_store"},
        )
        WatchlistRepository(session).insert_watchlist_event(
            event_id=event_id,
            owner_id=owner_id,
            watchlist_id="watchlist:graph_smoke",
            watchlist_item_id="watchlist_item:graph_smoke",
            asset_id=asset_id,
            event_type="candidate_intake_reason",
            from_status=None,
            to_status="active",
            reason="图谱冒烟入池原因。",
            source_decision_id=decision_id,
            created_at=now,
            payload={"memory_id": memory_id, "source": "smoke_graph_store"},
        )
        WatchlistRepository(session).insert_watchlist_event(
            event_id=similar_event_id,
            owner_id=owner_id,
            watchlist_id="watchlist:graph_smoke",
            watchlist_item_id="watchlist_item:graph_smoke_similar",
            asset_id=similar_asset_id,
            event_type="candidate_intake_reason",
            from_status=None,
            to_status="active",
            reason="图谱冒烟相似标的入池原因。",
            source_decision_id=similar_decision_id,
            created_at=now - timedelta(days=7),
            payload={"memory_id": similar_memory_id, "source": "smoke_graph_store"},
        )

        graph_store = DryRunGraphStore(settings=settings)
        schema_result = graph_store.initialize_schema()
        if not schema_result["initialized"]:
            raise AssertionError("DryRun GraphStore 必须返回初始化成功。")
        health = graph_store.health_check()
        if not health["healthy"]:
            raise AssertionError("DryRun GraphStore 健康检查必须成功。")

        sync_service = GraphSyncService(session=session, graph_store=graph_store)
        sync_result = sync_service.sync_asset_graph(
            owner_id=owner_id,
            asset_id=asset_id,
        )
        owner_sync = sync_service.sync_owner_graph(
            owner_id=owner_id,
            asset_ids=[asset_id, similar_asset_id],
        )
        if owner_sync["asset_count"] != 2:
            raise AssertionError("sync_owner_graph 必须支持多资产同步。")
        if sync_result.node_count < 7:
            raise AssertionError(
                "GraphSync 至少应生成 Asset、Decision、Memory、WatchlistEvent、"
                "Risk、Evidence、Review 节点。"
            )
        if sync_result.relationship_count < 7:
            raise AssertionError(
                "GraphSync 至少应生成 ABOUT、GENERATES、SUMMARIZED_BY、"
                "WARNED_BY、USES_EVIDENCE、REVIEWS 关系。"
            )

        runtime = FinanceToolRuntime(session, graph_store=graph_store)
        tool_names = set(runtime.list_tools())
        required_tools = {
            "memory.trace_asset_graph",
            "memory.explain_candidate_reason_chain",
            "memory.find_memory_conflicts",
            "memory.find_similar_decision_paths",
            "memory.detect_risk_contagion",
        }
        missing = required_tools - tool_names
        if missing:
            raise AssertionError(f"缺少图谱工具: {sorted(missing)}")

        trace = runtime.call(
            "memory.trace_asset_graph",
            owner_id=owner_id,
            asset_id=asset_id,
            max_depth=2,
        )
        if trace["graph_backend"] != "neo4j":
            raise AssertionError("图谱工具必须返回配置选择的后端。")
        if not trace["paths"]:
            raise AssertionError("图谱追踪必须返回路径摘要。")

        reason_chain = runtime.call(
            "memory.explain_candidate_reason_chain",
            owner_id=owner_id,
            asset_id=asset_id,
        )
        if not reason_chain["chains"]:
            raise AssertionError("入池原因链必须能从图谱投影中生成。")

        conflicts = runtime.call(
            "memory.find_memory_conflicts",
            owner_id=owner_id,
            asset_id=asset_id,
        )
        if not conflicts["conflicts"]:
            raise AssertionError("图谱冲突工具必须发现正反记忆冲突。")

        similar_paths = runtime.call(
            "memory.find_similar_decision_paths",
            owner_id=owner_id,
            asset_id=asset_id,
        )
        if not similar_paths["paths"]:
            raise AssertionError("相似历史决策工具必须返回路径。")

        risk_contagion = runtime.call(
            "memory.detect_risk_contagion",
            owner_id=owner_id,
            asset_id=asset_id,
            max_depth=3,
        )
        if not risk_contagion["paths"]:
            raise AssertionError("风险传导工具必须返回路径。")

        interface = FinanceAgentInterface(session)
        if interface.graph_health().data["backend"] != "neo4j":
            raise AssertionError("接口层 graph_health 必须返回当前后端。")
        if not interface.graph_find_similar_decision_paths(
            owner_id=owner_id,
            asset_id=asset_id,
        ).data["result"]["paths"]:
            raise AssertionError("接口层必须暴露相似决策路径。")

        verify_cli_graph_commands(owner_id=owner_id, asset_id=asset_id)
        verify_mcp_graph_tools()

        print(
            {
                "backend": settings.backend,
                "nodes": sync_result.node_count,
                "relationships": sync_result.relationship_count,
                "path_count": len(trace["paths"]),
                "chain_count": len(reason_chain["chains"]),
                "conflict_count": len(conflicts["conflicts"]),
                "similar_path_count": len(similar_paths["paths"]),
                "risk_path_count": len(risk_contagion["paths"]),
                "owner_sync_assets": owner_sync["asset_count"],
            }
        )


def verify_config_file_backend_selection() -> None:
    """验证图谱后端可以通过配置文件显式二选一。"""

    graph_env_keys = [
        "FINANCE_AGENT_GRAPH_BACKEND",
        "FINANCE_AGENT_GRAPH_ENABLED",
        "FINANCE_AGENT_GRAPH_DRY_RUN",
        "FINANCE_AGENT_GRAPH_CONFIG_FILE",
    ]
    previous_values = {key: os.environ.pop(key, None) for key in graph_env_keys}
    try:
        with tempfile.TemporaryDirectory() as temporary_dir:
            config_path = Path(temporary_dir) / "graph.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[graph]",
                        'backend = "apache_age"',
                        "enabled = true",
                        "dry_run = true",
                        "",
                        "[graph.apache_age]",
                        'graph_name = "finance_memory_graph_smoke"',
                    ]
                ),
                encoding="utf-8",
            )
            settings = load_graph_store_settings(config_file=str(config_path))
            if settings.backend != "apache_age":
                raise AssertionError("图谱配置文件必须能显式选择 apache_age 后端。")
            if not settings.enabled or not settings.dry_run:
                raise AssertionError("图谱配置文件必须能设置 enabled 和 dry_run。")
    finally:
        for key, value in previous_values.items():
            if value is not None:
                os.environ[key] = value


def verify_cli_graph_commands(*, owner_id: str, asset_id: str) -> None:
    """验证 CLI graph 命令组能调用接口层。"""

    commands = [
        ["graph", "health"],
        ["graph", "sync-asset", "--owner-id", owner_id, "--asset-id", asset_id],
        ["graph", "trace", "--owner-id", owner_id, "--asset-id", asset_id],
        ["graph", "reason-chain", "--owner-id", owner_id, "--asset-id", asset_id],
        ["graph", "similar-decisions", "--owner-id", owner_id, "--asset-id", asset_id],
        ["graph", "risk-contagion", "--owner-id", owner_id, "--asset-id", asset_id],
        ["graph", "conflicts", "--owner-id", owner_id, "--asset-id", asset_id],
    ]
    for command in commands:
        process = subprocess.run(
            [sys.executable, "-m", "finance_agent.cli", *command],
            check=True,
            capture_output=True,
            encoding="utf-8",
            text=True,
        )
        if '"status": "ok"' not in process.stdout:
            raise AssertionError(f"CLI graph 命令未返回 ok：{' '.join(command)}")


def verify_mcp_graph_tools() -> None:
    """验证 MCP Server 暴露图谱工具。"""

    server = create_mcp_server()
    tool_manager = getattr(server, "_tool_manager", None)
    if tool_manager is None:
        raise AssertionError("FastMCP Server 必须能访问工具管理器用于 smoke 验证。")
    tool_names = {tool.name for tool in tool_manager.list_tools()}
    required = {
        "graph_health",
        "graph_initialize",
        "graph_sync_asset",
        "graph_sync_owner",
        "graph_trace_asset",
        "graph_explain_candidate_reason_chain",
        "graph_find_similar_decision_paths",
        "graph_detect_risk_contagion",
        "graph_find_memory_conflicts",
    }
    missing = required - tool_names
    if missing:
        raise AssertionError(f"MCP 缺少图谱工具：{sorted(missing)}")


if __name__ == "__main__":
    main()
