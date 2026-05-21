"""验证 Finance Memory 工具、CLI 和 MCP 共享入口。"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.agents.tools.runtime import FinanceToolRuntime
from finance_agent.application import MemoryService
from finance_agent.storage.db import create_session_factory, get_database_url, session_scope
from finance_agent.storage.repositories import AssetRepository


def main() -> None:
    """执行 Finance Memory 工具入口冒烟验证。"""

    session_factory = create_session_factory()
    as_of = datetime.now(UTC).replace(microsecond=0)
    stamp = as_of.strftime("%Y%m%d%H%M%S")
    owner_id = f"owner:smoke:finance_memory_tools:{stamp}"
    asset_id = f"asset:smoke:finance_memory_tools:{stamp}:candidate"
    symbol = f"FMT{stamp[-6:]}"

    with session_scope(session_factory) as session:
        AssetRepository(session).upsert_asset(
            asset_id=asset_id,
            symbol=symbol,
            name="Finance Memory 工具候选",
            market="ashare",
            asset_type="stock",
            exchange="SSE",
            currency="CNY",
            payload={"source": "smoke_finance_memory_tools"},
        )
        memory = MemoryService(session)
        memory.upsert_memory(
            memory_id=f"memory:{stamp}:tool:intake",
            owner_id=owner_id,
            memory_type="candidate_intake_reason",
            scope="asset",
            asset_id=asset_id,
            content=f"{symbol} 入池原因：突破平台上沿后回踩确认，资金流继续为正。",
            confidence=Decimal("0.880000"),
            payload={"source": "smoke_finance_memory_tools"},
        )
        memory.upsert_memory(
            memory_id=f"memory:{stamp}:tool:daily",
            owner_id=owner_id,
            memory_type="watchlist_daily_reason",
            scope="asset",
            asset_id=asset_id,
            content=f"{symbol} 今日继续关注原因：回踩不破且波动收敛。",
            confidence=Decimal("0.820000"),
            payload={"source": "smoke_finance_memory_tools"},
        )

        runtime = FinanceToolRuntime(session)
        required_tools = {
            "memory.recall_asset_memories",
            "memory.get_asset_memory_context",
            "memory.get_asset_memory_timeline",
        }
        if not required_tools.issubset(set(runtime.list_tools())):
            raise AssertionError(f"Finance Memory 工具必须注册完整，实际={runtime.list_tools()}")

        context = runtime.call(
            "memory.get_asset_memory_context",
            owner_id=owner_id,
            asset_id=asset_id,
            query="回踩确认 资金流 继续关注",
            limit=5,
        )
        if not context["similar_memories"] or not context["timeline"]:
            raise AssertionError(f"工具必须返回相似召回和时间线，实际={context}")
        recall = runtime.call(
            "memory.recall_asset_memories",
            owner_id=owner_id,
            asset_id=asset_id,
            query="突破 回踩确认",
            limit=5,
        )
        if not recall["similar_memories"] or not recall["timeline"]:
            raise AssertionError(f"兼容召回工具必须返回增强上下文，实际={recall}")

        interface = FinanceAgentInterface(session)
        interface_result = interface.memory_recall_asset_context(
            owner_id=owner_id,
            asset_id=asset_id,
            query="继续观察",
            limit=5,
        ).to_dict()
        if interface_result["status"] != "ok":
            raise AssertionError(f"共享接口必须可读取记忆上下文，实际={interface_result}")

    cli = run_cli_memory_recall(
        owner_id=owner_id,
        asset_id=asset_id,
        query="资金流 回踩确认",
    )
    if cli["status"] != "ok":
        raise AssertionError(f"CLI memory recall 必须成功，实际={cli}")
    cli_result = cli["data"]["result"]
    if not cli_result["similar_memories"] or not cli_result["timeline"]:
        raise AssertionError(f"CLI memory recall 必须返回相似召回和时间线，实际={cli}")

    print(
        {
            "owner_id": owner_id,
            "asset_id": asset_id,
            "tool_count": len(required_tools),
            "cli_similar_count": len(cli_result["similar_memories"]),
            "cli_timeline_count": len(cli_result["timeline"]),
        }
    )


def run_cli_memory_recall(*, owner_id: str, asset_id: str, query: str) -> dict[str, object]:
    """调用 CLI 验证 memory recall 命令。"""

    command = [
        str(Path(".venv/Scripts/python.exe")),
        "-m",
        "finance_agent.cli",
        "--database-url",
        get_database_url(),
        "memory",
        "recall",
        "--owner-id",
        owner_id,
        "--asset-id",
        asset_id,
        "--query",
        query,
        "--limit",
        "5",
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


if __name__ == "__main__":
    main()
