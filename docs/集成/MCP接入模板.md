# MCP 接入模板

> 版本：2026-06-12  
> 用途：给 Hermes、Claude Code、Codex 或其他通用 MCP 客户端注册 `finance-agent` 金融工具入口。

## 1. 服务定位

`finance-agent` MCP Server 是给上层 Agent 使用的正式金融工具入口。它只做薄封装：

- Workflow 调度与报告查询通过 `FinanceAgentInterface`。
- 事实工具调用通过 `FinanceToolRuntime`，只读取已入库事实。
- 触发事件通过 `TriggerService`。
- 图谱和记忆工具只访问本项目 Finance Memory 与可重建图谱投影。

MCP Server 不直接抓取 AKShare、Binance、ccxt 或网页，也不提供真实交易下单能力。

## 2. 通用启动命令

Windows 项目路径：

```powershell
D:\Code\aiAgents\finance-agent
```

MCP Server 命令：

```powershell
D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe -m finance_agent.mcp_server
```

推荐环境变量：

```powershell
PYTHONIOENCODING=utf-8
```

## 3. Hermes 注册示例

Hermes 可按 stdio MCP server 注册：

```json
{
  "mcpServers": {
    "finance-agent": {
      "command": "D:\\Code\\aiAgents\\finance-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "finance_agent.mcp_server"],
      "cwd": "D:\\Code\\aiAgents\\finance-agent",
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

Hermes 侧调用原则：

- 需要分析时优先调用 `run_workflow`。
- 需要解释已有结果时调用 `get_report` 或 `get_workflow_run`。
- 需要事实补充时先调用 `list_tools`，再通过 `call_tool` 调用只读事实工具。
- 不直接联网查行情，不直接写交易结果。

## 4. Claude Code / Codex 注册示例

```toml
[mcp_servers.finance-agent]
type = "stdio"
command = "D:\\Code\\aiAgents\\finance-agent\\.venv\\Scripts\\python.exe"
args = ["-m", "finance_agent.mcp_server"]
cwd = "D:\\Code\\aiAgents\\finance-agent"
startup_timeout_sec = 120

[mcp_servers.finance-agent.env]
PYTHONIOENCODING = "utf-8"
```

如果客户端在 WSL 中运行，但需要调用 Windows venv，可使用 PowerShell 桥接；优先参考 `scripts/integration/smoke_hermes_skill_commands.py --mode wsl-bridge` 的命令构造。

## 5. 通用 MCP 客户端示例

```json
{
  "servers": {
    "finance-agent": {
      "transport": "stdio",
      "command": "D:\\Code\\aiAgents\\finance-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "finance_agent.mcp_server"],
      "cwd": "D:\\Code\\aiAgents\\finance-agent",
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## 6. 必备工具清单

MCP wrapper 工具至少应包含：

- `list_workflows`
- `run_workflow`
- `get_workflow_run`
- `get_report`
- `list_tools`
- `call_tool`
- `evaluate_triggers`
- `dispatch_triggers`
- `run_triggers_once`
- `graph_health`
- `memory_recall_asset_context`
- `memory_get_asset_timeline`

其中 `list_tools` 返回的是金融事实工具清单，里面的每个工具都必须带 `read_only: true`。

## 7. 验收命令

```powershell
.\.venv\Scripts\python.exe scripts\integration\smoke_mcp_handshake.py
```

脚本会启动 MCP Server 子进程，完成 initialize → list_tools，并调用 MCP 的 `list_tools` wrapper 校验内部事实工具清单只读。
