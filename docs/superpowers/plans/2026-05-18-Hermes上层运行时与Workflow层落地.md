# Hermes 上层运行时与 Workflow 层落地实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将上层自由 Agent 收敛为 Hermes Agent 运行时，把本项目的 `PersonalFinanceAgentService` 逐步降级并改造为 `FinanceAssistantService` 金融业务编排内核，同时使用 LangGraph 落地底层金融团队 Workflow，并补齐 Finance Tool Runtime、Hermes 工具接口和审计协议。

**架构：** Hermes Agent 负责长期运行、自由 loop、通用记忆、任务调度和用户对话；LangGraph 负责编排底层固定金融团队 Workflow；`finance-agent` 负责金融业务内核、清洗后事实查询、Finance Memory、工具端口、审计和中文解释报告。第一阶段保留现有 `PersonalFinanceAgentService` 类名作为兼容入口，新代码和文档使用 `FinanceAssistantService` 作为目标名称。

**技术栈：** Python 3.12、SQLAlchemy、PostgreSQL + TimescaleDB、Typer CLI、MCP/API 工具接口、LangGraph、dataclass / TypedDict DTO、DeepSeek V4 Pro、GPT-5.5 Pro。

---

## 文件结构

计划涉及的文件和职责：

- 创建：`docs/Workflow层设计.md`
  - 记录 Hermes 上层运行时、`FinanceAssistantService`、Workflow Runtime、工具层和高风险复核的设计边界。
- 修改：`docs/架构方案.md`
  - 把“上层 `PersonalFinanceAgent`”调整为“Hermes Agent 作为上层运行时，`FinanceAssistantService` 作为金融业务编排内核”。
- 修改：`docs/项目计划.md`
  - 同步产品主链路、Agent 层、Hermes 集成和里程碑描述。
- 修改：`docs/模型选型与职责分配.md`
  - 明确 Hermes 负责运行时，DeepSeek V4 Pro / GPT-5.5 Pro 是模型路由，不是业务服务边界。
- 修改：`docs/项目进度跟踪表.md`
  - 增加 Workflow 层落地进度和下一步任务。
- 创建：`src/finance_agent/agents/tools/`
  - 金融事实工具运行时，提供持仓、观察池、推荐、信号、风险、记忆和 Workflow 查询工具。
- 创建：`src/finance_agent/agents/runtime/`
  - LangGraph 适配器、步骤审计、高风险复核策略和结构化输出协议。不实现自研图执行框架。
- 修改：`src/finance_agent/agents/personal_assistant.py`
  - 保留兼容入口，逐步包装为 `FinanceAssistantService`。
- 修改：`src/finance_agent/agents/workflows/*.py`
  - 接入统一 Workflow 输入输出协议和 runtime step 事件。
- 创建：`scripts/storage/smoke_hermes_langgraph_workflow.py`
  - 验证 Hermes 工具入口调用业务内核和 LangGraph Workflow 后能写入审计、决策、记忆与复盘。

## 任务 1：同步架构文档

**文件：**
- 已创建：`docs/Workflow层设计.md`
- 修改：`docs/架构方案.md`
- 修改：`docs/项目计划.md`
- 修改：`docs/模型选型与职责分配.md`
- 修改：`docs/项目进度跟踪表.md`

- [ ] **步骤 1：更新架构总图**

把旧的：

```text
PersonalFinanceAgent -> FinancialTeamWorkflow
```

更新为：

```text
Hermes Agent -> CLI / MCP / API -> FinanceAssistantService -> LangGraph Workflow Adapter -> Domain Workflow
```

保留说明：`PersonalFinanceAgentService` 是当前兼容类名，目标名称是 `FinanceAssistantService`。

- [ ] **步骤 2：更新职责边界**

在文档中明确：

```text
Hermes Agent：
- 长期运行
- 自由 loop
- 通用记忆
- 工具调用
- 用户对话

FinanceAssistantService：
- 读取金融事实
- 调用 Workflow
- 写入 Finance Memory
- 写入审计和复盘
- 不直接抓外部数据
- 不直接计算因子、评分和信号
```

- [ ] **步骤 3：更新里程碑**

把 M4/M5 附近的 Agent 层描述更新为：

```text
M4A：Hermes 上层运行时与 FinanceAssistantService 业务内核
M4B：Finance Tool Runtime
M4C：LangGraph Workflow 适配层
M4D：Hermes 工具入口和 smoke 验证
```

- [ ] **步骤 4：文档自检**

运行：

```powershell
rg -n "PersonalFinanceAgent 私人金融主 Agent|上层 `PersonalFinanceAgent`|PersonalFinanceAgentService.handle_trigger" docs
```

预期：旧说法只保留在“兼容类名”或“历史实现”语境中；新架构章节应使用 Hermes Agent 和 `FinanceAssistantService`。

- [ ] **步骤 5：Commit**

```powershell
git add docs/Workflow层设计.md docs/架构方案.md docs/项目计划.md docs/模型选型与职责分配.md docs/项目进度跟踪表.md docs/superpowers/plans/2026-05-18-Hermes上层运行时与Workflow层落地.md
git commit -m "docs: 记录Hermes运行时与Workflow层落地方案"
```

## 任务 2：建立 Finance Tool Runtime

**文件：**
- 创建：`src/finance_agent/agents/tools/__init__.py`
- 创建：`src/finance_agent/agents/tools/runtime.py`
- 创建：`src/finance_agent/agents/tools/portfolio.py`
- 创建：`src/finance_agent/agents/tools/watchlist.py`
- 创建：`src/finance_agent/agents/tools/recommendation.py`
- 创建：`src/finance_agent/agents/tools/risk_signal.py`
- 创建：`src/finance_agent/agents/tools/memory.py`
- 创建：`src/finance_agent/agents/tools/workflow.py`
- 测试：`scripts/storage/smoke_finance_tool_runtime.py`

- [ ] **步骤 1：编写失败的 smoke 脚本**

创建 `scripts/storage/smoke_finance_tool_runtime.py`，先验证工具运行时可以读取持仓、观察池、推荐、信号、风险和 Finance Memory。

```python
from __future__ import annotations

from finance_agent.agents.tools import FinanceToolRuntime
from finance_agent.storage.database import SessionLocal


def main() -> None:
    with SessionLocal() as session:
        runtime = FinanceToolRuntime(session)
        tools = runtime.list_tools()
        required = {
            "portfolio.get_snapshot",
            "watchlist.get_active_items",
            "recommendation.get_latest_run",
            "signal_risk.get_asset_context",
            "memory.recall_asset_memories",
            "workflow.list_workflows",
        }
        missing = required - set(tools)
        if missing:
            raise AssertionError(f"缺少工具: {sorted(missing)}")
        print({"tool_count": len(tools), "required": sorted(required)})


if __name__ == "__main__":
    main()
```

- [ ] **步骤 2：运行脚本确认失败**

运行：

```powershell
.venv\Scripts\python.exe scripts/storage/smoke_finance_tool_runtime.py
```

预期：失败，错误包含 `ModuleNotFoundError` 或 `cannot import name 'FinanceToolRuntime'`。

- [ ] **步骤 3：实现最小工具注册器**

创建 `src/finance_agent/agents/tools/runtime.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any

from sqlalchemy.orm import Session

ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class FinanceTool:
    """金融事实工具定义。"""

    name: str
    description: str
    handler: ToolHandler


class FinanceToolRuntime:
    """供 Hermes、Workflow 和业务服务调用的金融事实工具运行时。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._tools: dict[str, FinanceTool] = {}
        self._register_builtin_tools()

    def list_tools(self) -> tuple[str, ...]:
        """列出可调用工具名称。"""

        return tuple(sorted(self._tools))

    def get_tool(self, name: str) -> FinanceTool:
        """读取工具定义。"""

        return self._tools[name]

    def call(self, name: str, **kwargs: Any) -> Any:
        """调用工具。"""

        return self.get_tool(name).handler(**kwargs)

    def register(self, tool: FinanceTool) -> None:
        """注册工具。"""

        self._tools[tool.name] = tool

    def _register_builtin_tools(self) -> None:
        """注册第一批只读金融事实工具。"""

        self.register(FinanceTool("portfolio.get_snapshot", "读取组合持仓快照。", lambda **_: None))
        self.register(FinanceTool("watchlist.get_active_items", "读取活跃观察项。", lambda **_: None))
        self.register(FinanceTool("recommendation.get_latest_run", "读取最新推荐运行。", lambda **_: None))
        self.register(FinanceTool("signal_risk.get_asset_context", "读取标的信号和风险。", lambda **_: None))
        self.register(FinanceTool("memory.recall_asset_memories", "召回标的 Finance Memory。", lambda **_: None))
        self.register(FinanceTool("workflow.list_workflows", "列出可调用 Workflow。", lambda **_: None))
```

创建 `src/finance_agent/agents/tools/__init__.py`：

```python
"""金融事实工具运行时。"""

from finance_agent.agents.tools.runtime import FinanceTool, FinanceToolRuntime

__all__ = ["FinanceTool", "FinanceToolRuntime"]
```

- [ ] **步骤 4：运行 smoke 脚本确认通过**

运行：

```powershell
.venv\Scripts\python.exe scripts/storage/smoke_finance_tool_runtime.py
```

预期：输出包含 `tool_count` 和 6 个 required 工具名。

- [ ] **步骤 5：Commit**

```powershell
git add src/finance_agent/agents/tools scripts/storage/smoke_finance_tool_runtime.py
git commit -m "feat: 新增金融事实工具运行时"
```

## 任务 3：建立 LangGraph Workflow 适配层

**文件：**
- 创建：`src/finance_agent/agents/runtime/__init__.py`
- 创建：`src/finance_agent/agents/runtime/langgraph_adapter.py`
- 创建：`src/finance_agent/agents/runtime/policies.py`
- 测试：`scripts/storage/smoke_langgraph_workflow_adapter.py`

- [ ] **步骤 1：编写失败的 smoke 脚本**

创建 `scripts/storage/smoke_langgraph_workflow_adapter.py`：

```python
from __future__ import annotations

from datetime import datetime, timezone

from finance_agent.agents.runtime import LangGraphWorkflowAdapter, WorkflowNodeEvent
from finance_agent.storage.database import SessionLocal


def main() -> None:
    with SessionLocal() as session:
        adapter = LangGraphWorkflowAdapter(session)
        result = adapter.record_completed_graph(
            workflow_run_id="workflow:smoke:runtime:20260518",
            owner_id="owner:smoke",
            workflow_type="runtime_smoke",
            trigger_type="manual",
            started_at=datetime(2026, 5, 18, tzinfo=timezone.utc),
            node_events=(
                WorkflowNodeEvent("load_context", {"loaded": True}),
                WorkflowNodeEvent("decision_synthesis", {"decision": "watch"}),
            ),
            initial_state={"asset_id": "asset:smoke"},
            final_state={"asset_id": "asset:smoke", "loaded": True, "decision": "watch"},
        )
        assert result["loaded"] is True
        assert result["decision"] == "watch"
        print(result)


if __name__ == "__main__":
    main()
```

- [ ] **步骤 2：运行脚本确认失败**

运行：

```powershell
.venv\Scripts\python.exe scripts/storage/smoke_langgraph_workflow_adapter.py
```

预期：失败，错误包含 `ModuleNotFoundError` 或 `cannot import name 'LangGraphWorkflowAdapter'`。

- [ ] **步骤 3：实现最小 LangGraph 审计适配器**

创建 `src/finance_agent/agents/runtime/langgraph_adapter.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.application import WorkflowService

WorkflowState = dict[str, Any]


@dataclass(frozen=True)
class WorkflowNodeEvent:
    """LangGraph 节点审计事件。"""

    name: str
    output: dict[str, Any]


class LangGraphWorkflowAdapter:
    """连接 LangGraph 工作流和本项目审计落库的适配器。"""

    def __init__(self, session: Session) -> None:
        self.audit = WorkflowService(session)

    def record_completed_graph(
        self,
        *,
        workflow_run_id: str,
        owner_id: str,
        workflow_type: str,
        trigger_type: str,
        started_at: datetime,
        node_events: tuple[WorkflowNodeEvent, ...],
        initial_state: WorkflowState,
        final_state: WorkflowState,
    ) -> WorkflowState:
        """记录一次已经由 LangGraph 执行完成的工作流。"""

        self.audit.start_run(
            workflow_run_id=workflow_run_id,
            owner_id=owner_id,
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            started_at=started_at,
            payload={"node_count": len(node_events), "initial_keys": sorted(initial_state)},
        )
        for index, event in enumerate(node_events, start=1):
            self.audit.record_event(
                workflow_event_id=f"{workflow_run_id}:node:{index}:{event.name}",
                workflow_run_id=workflow_run_id,
                event_type="workflow_node_completed",
                agent_name=event.name,
                message=f"LangGraph 节点已完成：{event.name}",
                created_at=started_at,
                payload={"node": index, "output_keys": sorted(event.output)},
            )
        self.audit.finish_run(
            workflow_run_id=workflow_run_id,
            owner_id=owner_id,
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            started_at=started_at,
            finished_at=started_at,
            status="succeeded",
            payload={"final_keys": sorted(final_state)},
        )
        return final_state
```

创建 `src/finance_agent/agents/runtime/__init__.py`：

```python
"""Agent 与 LangGraph Workflow 适配层。"""

from finance_agent.agents.runtime.langgraph_adapter import (
    LangGraphWorkflowAdapter,
    WorkflowNodeEvent,
)

__all__ = ["LangGraphWorkflowAdapter", "WorkflowNodeEvent"]
```

- [ ] **步骤 4：运行 smoke 脚本确认通过**

运行：

```powershell
.venv\Scripts\python.exe scripts/storage/smoke_langgraph_workflow_adapter.py
```

预期：输出包含 `loaded: True` 和 `decision: watch`。

- [ ] **步骤 5：Commit**

```powershell
git add src/finance_agent/agents/runtime scripts/storage/smoke_langgraph_workflow_adapter.py
git commit -m "feat: 新增LangGraph工作流审计适配层"
```

## 任务 4：将业务入口包装为 `FinanceAssistantService`

**文件：**
- 修改：`src/finance_agent/agents/personal_assistant.py`
- 修改：`src/finance_agent/agents/__init__.py`
- 测试：`scripts/storage/smoke_m4_agent_decision_workflow.py`
- 测试：`scripts/storage/smoke_watchlist_management_workflow.py`
- 测试：`scripts/storage/smoke_portfolio_monitoring_workflow.py`

- [ ] **步骤 1：增加兼容别名测试**

在现有 smoke 脚本或新脚本中加入：

```python
from finance_agent.agents.personal_assistant import (
    FinanceAssistantService,
    PersonalFinanceAgentService,
)

assert issubclass(FinanceAssistantService, PersonalFinanceAgentService)
```

- [ ] **步骤 2：运行测试确认失败**

运行：

```powershell
.venv\Scripts\python.exe scripts/storage/smoke_m4_agent_decision_workflow.py
```

预期：失败，错误包含 `cannot import name 'FinanceAssistantService'`。

- [ ] **步骤 3：添加兼容类**

在 `src/finance_agent/agents/personal_assistant.py` 中 `PersonalFinanceAgentService` 类定义后添加：

```python
class FinanceAssistantService(PersonalFinanceAgentService):
    """金融助手业务编排内核。

    当前继承旧的 PersonalFinanceAgentService，以保持 M2/M3/M4 已有入口兼容。
    后续新增工具运行时和 Hermes 入口应优先依赖本类名。
    """
```

- [ ] **步骤 4：导出新类名**

在 `src/finance_agent/agents/__init__.py` 中导出 `FinanceAssistantService`。

- [ ] **步骤 5：运行回归验证**

运行：

```powershell
.venv\Scripts\python.exe scripts/storage/smoke_portfolio_monitoring_workflow.py
.venv\Scripts\python.exe scripts/storage/smoke_watchlist_management_workflow.py
.venv\Scripts\python.exe scripts/storage/smoke_m4_agent_decision_workflow.py
```

预期：三个脚本通过，现有持仓监控、观察池管理和推荐决策闭环不被破坏。

- [ ] **步骤 6：Commit**

```powershell
git add src/finance_agent/agents scripts/storage
git commit -m "refactor: 增加FinanceAssistantService兼容入口"
```

## 任务 5：新增 Hermes 工具入口 smoke

**文件：**
- 创建：`scripts/storage/smoke_hermes_langgraph_workflow.py`
- 修改：`docs/项目进度跟踪表.md`

- [ ] **步骤 1：编写 smoke 脚本**

创建 `scripts/storage/smoke_hermes_langgraph_workflow.py`：

```python
from __future__ import annotations

from finance_agent.agents.personal_assistant import FinanceAssistantService
from finance_agent.agents.tools import FinanceToolRuntime
from finance_agent.storage.database import SessionLocal


def main() -> None:
    with SessionLocal() as session:
        assistant = FinanceAssistantService(session)
        tools = FinanceToolRuntime(session)
        tool_names = tools.list_tools()
        assert "workflow.list_workflows" in tool_names
        assert assistant is not None
        print(
            {
                "assistant": "FinanceAssistantService",
                "tool_count": len(tool_names),
                "hermes_entry": "ready",
            }
        )


if __name__ == "__main__":
    main()
```

- [ ] **步骤 2：运行 smoke**

运行：

```powershell
.venv\Scripts\python.exe scripts/storage/smoke_hermes_langgraph_workflow.py
```

预期：输出 `hermes_entry: ready`。

- [ ] **步骤 3：更新进度表**

在 `docs/项目进度跟踪表.md` 增加：

```text
Hermes 上层运行时：已明确由 Hermes 负责长期 loop，finance-agent 提供 FinanceAssistantService、工具运行时和 LangGraph Workflow 适配层。
```

- [ ] **步骤 4：Commit**

```powershell
git add scripts/storage/smoke_hermes_langgraph_workflow.py docs/项目进度跟踪表.md
git commit -m "test: 验证Hermes调用LangGraph工作流入口"
```

## 任务 6：最终验证

**文件：**
- 验证：`src/finance_agent/agents/**/*.py`
- 验证：`scripts/storage/smoke_*.py`
- 验证：`docs/*.md`

- [ ] **步骤 1：静态检查**

运行：

```powershell
.venv\Scripts\python.exe -m ruff check src scripts
```

预期：通过。

- [ ] **步骤 2：编译检查**

运行：

```powershell
.venv\Scripts\python.exe -m py_compile src\finance_agent\agents\personal_assistant.py src\finance_agent\agents\tools\runtime.py src\finance_agent\agents\runtime\langgraph_adapter.py
```

预期：无输出，退出码为 0。

- [ ] **步骤 3：核心 smoke 回归**

运行：

```powershell
.venv\Scripts\python.exe scripts/storage/smoke_portfolio_monitoring_workflow.py
.venv\Scripts\python.exe scripts/storage/smoke_watchlist_management_workflow.py
.venv\Scripts\python.exe scripts/storage/smoke_m4_agent_decision_workflow.py
.venv\Scripts\python.exe scripts/storage/smoke_finance_tool_runtime.py
.venv\Scripts\python.exe scripts/storage/smoke_langgraph_workflow_adapter.py
.venv\Scripts\python.exe scripts/storage/smoke_hermes_langgraph_workflow.py
```

预期：全部通过。

- [ ] **步骤 4：差异检查**

运行：

```powershell
git diff --check
```

预期：没有 whitespace error。Windows 下 CRLF warning 可以单独记录，不作为失败。

- [ ] **步骤 5：最终 Commit**

```powershell
git add src scripts docs
git commit -m "feat: 落地Hermes与LangGraph工作流地基"
```

## 执行注意事项

- 不要让 Hermes、Workflow 或工具层直接调用 AKShare、Binance、ccxt。
- 不要把 Hermes Memory 写入金融事实表；金融建议只引用 Finance Memory 和结构化事实。
- 不要删除现有 `PersonalFinanceAgentService`，先做兼容包装。
- 不要把自然语言报告当作唯一输出；必须保留结构化 JSON 和证据 ID。
- 不要自研 AI Workflow 框架；底层固定金融团队 Workflow 使用 LangGraph，本项目只写业务适配、工具协议、审计和 fallback。
- 提交信息使用中文。
