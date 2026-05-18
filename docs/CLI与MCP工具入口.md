# CLI 与 MCP 工具入口

本文记录当前阶段给 Hermes-Agent、Codex、Scheduler 和后续前端 API 使用的工具入口。结论是：CLI 和 MCP 都保持薄入口；Workflow 与事实工具共用 `FinanceAgentInterface`，V1.2 触发事件入口调用 `TriggerService` 并唤醒 Hermes-Agent 或内部金融 Agent，入口层只做参数解析、事务边界、JSON 序列化和调用转发，不承载金融决策逻辑。

## 1. 分层

```mermaid
flowchart TD
    H["Hermes / Codex / Scheduler"] --> CLI["CLI\nfinance-agent"]
    H --> MCP["MCP Server\nfinance-agent-mcp"]
    CLI --> IF["FinanceAgentInterface\nWorkflow / 工具门面"]
    MCP --> IF
    CLI --> TS["TriggerService\n触发事件评估与派发"]
    MCP --> TS
    IF --> FAS["FinanceAssistantService\n金融业务编排内核"]
    IF --> TR["FinanceToolRuntime\n只读事实工具"]
    TS --> AG["Hermes-Agent / 内部金融 Agent\n触发唤醒目标"]
    AG --> FAS
    TS --> EVT["assistant_trigger_events"]
    FAS --> WF["LangGraph Workflow"]
    TR --> DB["PostgreSQL + TimescaleDB\n已清洗入库数据"]
    WF --> LOG["agent_workflow_runs / agent_workflow_events"]
```

## 2. CLI 入口

CLI 入口：

```bash
python -m finance_agent.cli workflows list
python -m finance_agent.cli tools list
```

安装为 console script 后也可以使用：

```bash
finance-agent workflows list
finance-agent tools list
```

运行报告类 Workflow 示例：

```bash
finance-agent workflows run asset_deep_analysis \
  --owner-id owner:demo \
  --asset-id asset:demo:600519 \
  --portfolio-id portfolio:demo \
  --watchlist-id watchlist:demo
```

运行推荐决策 Workflow 示例：

```bash
finance-agent workflows run recommendation_decision \
  --owner-id owner:demo \
  --portfolio-id portfolio:demo \
  --watchlist-id watchlist:demo \
  --recommendation-run-id run:demo:ashare:latest
```

读取审计和报告：

```bash
finance-agent workflows show workflow:demo:asset_deep_analysis:20260518160000
finance-agent reports show workflow:demo:asset_deep_analysis:20260518160000
finance-agent reports show workflow:demo:asset_deep_analysis:20260518160000 --markdown
```

调用只读事实工具：

```bash
finance-agent tools call factor.get_asset_factor_context \
  --arguments "{\"asset_id\":\"asset:demo:600519\",\"horizon\":\"swing\"}"
```

CLI 默认输出结构化 JSON；`reports show --markdown` 可只输出中文 Markdown 正文。

## 3. MCP 入口

MCP Server 入口：

```bash
python -m finance_agent.mcp_server
finance-agent-mcp
```

当前 MCP tools：

| MCP Tool | 作用 |
| --- | --- |
| `list_workflows` | 列出 6 个金融团队 Workflow |
| `run_workflow` | 运行 Workflow，返回结构化结果和报告 |
| `get_workflow_run` | 查询 Workflow run 和审计事件 |
| `get_report` | 查询中文解释报告，可返回 Markdown |
| `list_tools` | 列出只读金融事实工具 |
| `call_tool` | 调用 `FinanceToolRuntime` 中的只读事实工具 |
| `evaluate_triggers` | 评估已入库事实并生成触发事件 |
| `dispatch_triggers` | 派发待处理触发事件到 Agent 唤醒队列 |
| `run_triggers_once` | 执行一次触发评估并立即唤醒 Agent |

MCP 依赖写入 `pyproject.toml`：`mcp>=1.16,<2.0`。当前本地已验证 `mcp 1.27.1` 可以创建 server。

## 4. 约束

- CLI 和 MCP 不直接调用 AKShare、Binance、ccxt 或网页。
- CLI 和 MCP 不直接计算指标、因子、评分或信号。
- CLI 和 MCP 不直接调用外部 LLM。
- CLI 和 MCP 不绕过 `FinanceAssistantService` 写决策、记忆或审计。
- 触发入口不直接给买卖结论，也不直接运行 Workflow；只写 `assistant_trigger_events` 并唤醒 Hermes-Agent 或内部金融 Agent。
- 真实交易下单仍不在本项目第一阶段范围内，后续必须增加人工确认和交易权限开关。

## 5. 验证

当前验证脚本：

```bash
python scripts/storage/smoke_agent_cli_interface.py
python scripts/storage/smoke_agent_mcp_server.py
python scripts/storage/smoke_v12_trigger_events.py
```

已验证：

- `FinanceAgentInterface` 和 CLI 暴露一致的 Workflow 清单。
- CLI 可以输出结构化 JSON。
- CLI 可以运行 `asset_deep_analysis` 圆桌报告 Workflow。
- MCP SDK 安装后可以创建 MCP Server。
- V1.2 触发层能生成 6 类触发事件、冷却去重，并通过 CLI 重复评估返回结构化 JSON。
