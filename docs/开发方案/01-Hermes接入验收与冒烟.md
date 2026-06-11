# 方案 01：Hermes 接入验收与冒烟（O1 收尾）

> 优先级：P0 ｜ 批次 1 ｜ 依赖：无
> 前置阅读：`00-总体规划与执行约定.md`

## 1. 背景与现状

Hermes 侧的 skill 已经编写完成（`finance-agent-recommendation`），内容包括：

- 定位与职责边界（不下单、不在 Hermes 内抓行情/算因子、不覆盖确定性分数）。
- 项目入口：WSL 路径 `/mnt/d/Code/aiAgents/finance-agent`，通过 `powershell.exe -NoProfile -ExecutionPolicy Bypass` 调用 Windows venv 里的 CLI。
- Workflow 选择表（recommendation_decision / asset_deep_analysis / portfolio_monitoring / watchlist_management / swap_decision / daily_review）。
- 输出规则（结论→证据→风险反驳→行动→确认边界）和交付前检查清单。
- 一个项目可见性检查脚本（检查 `pyproject.toml` 与 venv python 是否存在）。

仓库侧已具备的能力：

- `FinanceAgentInterface`（CLI/MCP 共用门面）。
- CLI：`finance-agent workflows list/run/show`、`tools list/call`、`reports show`、`graph health`、`agent run-once/run-loop`、`memory recall/timeline` 等（入口 `src/finance_agent/cli/main.py`）。
- MCP Server：`finance-agent-mcp`（`src/finance_agent/mcp_server/server.py`），暴露 `list_workflows`、`run_workflow`、`get_report`、`call_tool`、memory 与 graph 工具。

**缺口（本方案要补的）**：skill 写好了但从未被端到端验收过。`docs/优化版本进度跟踪表.md` 中 O1 的 5 个子任务（使用规范、CLI 配置、MCP 配置、权限边界、冒烟脚本）状态仍是"待开始"。skill 里的每条命令是否真的能在 WSL→PowerShell 链路上跑通、中文输出是否乱码、MCP 在 Hermes 中如何注册，都没有验证记录。

## 2. 目标与非目标

**目标**

1. skill 中列出的每条命令都有自动化冒烟验证，可重复执行。
2. WSL→PowerShell→venv 调用链的编码、退出码、JSON 可解析性得到保证。
3. MCP 接入有配置模板和验证脚本。
4. 权限边界经过审计：Hermes 可见的工具全部只读 + Workflow 白名单，无任何交易类入口。
5. skill 文档在本仓库有受版本管理的副本，避免 Hermes 侧与仓库脱节。

**非目标**

- 不改 Hermes 本体。
- 不在本批次新增业务功能（触发、复核等归后续批次）。

## 3. 技术方案

### 3.1 skill 副本入库

- 新增 `docs/集成/Hermes金融助手Skill规范.md`，内容与 Hermes 侧 skill 保持一致，并加一节"版本同步说明"（两边任何一边改动都要同步另一边，以仓库版为准源）。
- 把 Hermes 侧的可见性检查脚本入库为 `scripts/integration/check_hermes_visibility.py`（保持现有逻辑，路径常量改为可通过环境变量 `FINANCE_AGENT_PROJECT_ROOT` 覆盖，默认 `/mnt/d/Code/aiAgents/finance-agent`）。

### 3.2 端到端冒烟脚本

新增 `scripts/integration/smoke_hermes_skill_commands.py`，逐条执行 skill"常用命令"小节的命令并断言：

| 命令 | 断言 |
| --- | --- |
| `workflows list` | 退出码 0；stdout 是合法 JSON；包含 6 个 workflow 名 |
| `tools list` | 退出码 0；JSON；只读工具均存在；**不存在**任何名称含 `order`/`trade`/`execute` 的工具 |
| `models config` | 退出码 0；输出脱敏（断言不出现真实 key 形态字符串） |
| `graph health` | 退出码 0 或明确的"图谱未配置"降级输出（两者都算通过，记录状态） |
| `agent run-once --limit 1` | 退出码 0；无待处理任务时输出空结果而不是报错 |
| `workflows run asset_deep_analysis ...`（用冒烟 owner/asset） | 退出码 0；返回 `workflow_run_id` |
| `reports show <上一步run_id> --markdown` | 退出码 0；输出含中文报告关键 section（执行摘要、风险反驳） |

脚本要求：

- 支持两种运行模式：`--mode direct`（直接调 venv python，在 Windows 上跑）和 `--mode wsl-bridge`（通过 `powershell.exe` 调用，模拟 Hermes 实际链路；在 WSL 内运行时使用）。
- 每条命令记录耗时；任何一条失败立即输出失败命令的完整 stdout/stderr。
- 中文编码处理：调用子进程时设置 `PYTHONIOENCODING=utf-8`，PowerShell 命令前缀加 `[Console]::OutputEncoding=[Text.Encoding]::UTF8;`。如果实测仍有乱码，在 CLI 入口（`src/finance_agent/cli/main.py`）统一 `sys.stdout.reconfigure(encoding="utf-8")`。

### 3.3 MCP 接入模板与验证

- 仓库已有 `.ai/mcp/mcp.json`，以它为基础新增 `docs/集成/MCP接入模板.md`，给出 Hermes / Claude Code / 通用 MCP 客户端三种注册示例（命令均指向 `D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe -m finance_agent.mcp_server`）。
- 新增 `scripts/integration/smoke_mcp_handshake.py`：用 `mcp` SDK 客户端启动 server 子进程，完成 initialize → list_tools，断言工具清单与 `FinanceAgentInterface` 一致（复用既有 `smoke_agent_mcp_server.py` 的做法，但增加工具名单一致性断言）。

### 3.4 权限边界审计（一次性 + 防回归）

- 新增测试 `tests/test_hermes_tool_boundary.py`：
  - 遍历 `FinanceAgentInterface` 暴露的全部工具，断言其注册元数据均为只读（按工具命名空间白名单：`portfolio.`、`watchlist.`、`factor.`、`signal_risk.`、`memory.`、`graph.`、`data_quality.` 等，以 `tools list` 实际输出为准先盘点再固化）。
  - 断言 `run_workflow` 仅接受 6 个白名单 workflow 名，传入未知名称抛出明确错误。
  - 这是防回归测试：未来任何人新增写操作工具都会被此测试拦截，必须显式评审。

## 4. 任务拆解

- [ ] T1 入库 skill 副本与可见性检查脚本（3.1）。
- [ ] T2 编写 `tests/test_hermes_tool_boundary.py`（先盘点 `tools list` 实际输出，再写断言；TDD）。
- [ ] T3 编写 `smoke_hermes_skill_commands.py` 的 `--mode direct`，在 Windows 直跑通过。
- [ ] T4 补 `--mode wsl-bridge`；如遇编码问题按 3.2 修 CLI 入口（改 CLI 前先跑 gitnexus_impact）。
- [ ] T5 编写 MCP 模板文档与 `smoke_mcp_handshake.py`。
- [ ] T6 全量回归（pytest + 两个冒烟脚本），更新 `docs/优化版本进度跟踪表.md` O1 五个子任务状态和本文件进度表。

## 5. 验收命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_hermes_tool_boundary.py -q
.\.venv\Scripts\python.exe scripts\integration\smoke_hermes_skill_commands.py --mode direct
.\.venv\Scripts\python.exe scripts\integration\smoke_mcp_handshake.py
.\.venv\Scripts\python.exe -m pytest -q
```

WSL 侧（如可用）：

```bash
python3 scripts/integration/smoke_hermes_skill_commands.py --mode wsl-bridge
```

## 6. 风险与注意事项

- `workflows run` 冒烟依赖数据库中存在冒烟 owner/asset；脚本应先检查并在缺失时给出准备指引（参考 `scripts/storage/smoke_*` 既有做法），不要静默造数。
- `graph health` 在未配置 Neo4j 的环境会降级，冒烟脚本必须把"降级但结构化"视为通过，把"异常栈"视为失败。
- PowerShell 引号转义是 WSL 桥接的主要坑：统一用单引号包路径、双引号包整条 `-Command`，并在脚本里集中封装一个 `build_powershell_command()` 帮助函数，禁止散落拼字符串。

## 7. 进度表

| 任务 | 状态 | 验证记录 |
| --- | --- | --- |
| T1 skill 副本入库 | 已完成 | `.\.venv\Scripts\python.exe scripts\integration\check_hermes_visibility.py --project-root D:\Code\aiAgents\finance-agent` 通过，确认项目根、`pyproject.toml`、venv Python、CLI 与 MCP 入口可见 |
| T2 工具边界防回归测试 | 未开始 | - |
| T3 冒烟 direct 模式 | 未开始 | - |
| T4 冒烟 wsl-bridge 模式 | 未开始 | - |
| T5 MCP 模板与握手冒烟 | 未开始 | - |
| T6 回归与文档同步 | 未开始 | - |
