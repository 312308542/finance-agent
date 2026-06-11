# Hermes 金融助手 Skill 规范

> 版本：2026-06-12  
> 准源：本仓库版本为准源；Hermes 侧 `finance-agent-recommendation` skill 如有调整，必须同步回本文档。

## 1. 定位

`finance-agent-recommendation` 是给 Hermes-Agent 使用的金融业务 skill。Hermes 负责长期对话、任务调度、工具选择和通用记忆；`finance-agent` 负责已入库金融事实、Workflow 分析、Finance Memory、审计报告和权限边界。

Hermes 调用本项目时必须遵守以下原则：

- 不自动交易，不调用真实下单、券商写接口或交易所写接口。
- 不在 Hermes 内直接抓行情、算指标、算因子或覆盖确定性分数。
- 所有金融事实必须来自 `finance-agent` 已入库数据，优先通过 CLI 或 MCP 工具读取。
- 所有建议必须引用 Workflow 报告、因子/风险/信号证据、Finance Memory 或审计事件。
- 模型只能给出解释、比较、反驳和建议文本，不能修改确定性评分和风控标记。

## 2. 项目入口

Windows 项目路径：

```powershell
D:\Code\aiAgents\finance-agent
```

WSL 路径：

```bash
/mnt/d/Code/aiAgents/finance-agent
```

Windows venv Python：

```powershell
D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe
```

Hermes 在 WSL 中调用 Windows CLI 时，统一通过 PowerShell 桥接：

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \
  "[Console]::OutputEncoding=[Text.Encoding]::UTF8; cd 'D:\Code\aiAgents\finance-agent'; .\.venv\Scripts\python.exe -m finance_agent.cli workflows list"
```

所有桥接命令必须设置 UTF-8 输出，避免中文报告乱码。

## 3. 常用命令

列出可用 Workflow：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli workflows list
```

列出可用事实工具：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli tools list
```

查看模型配置：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli models config
```

检查图谱健康：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli graph health
```

消费一次待处理 Agent 任务：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli agent run-once --limit 1
```

运行单标的深度分析：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli workflows run asset_deep_analysis `
  --owner-id owner:demo `
  --asset-id asset:demo:600519 `
  --portfolio-id portfolio:demo `
  --watchlist-id watchlist:demo
```

读取 Workflow 报告：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli reports show <workflow_run_id> --markdown
```

## 4. Workflow 选择表

| 用户意图 | Workflow | 使用场景 |
| --- | --- | --- |
| 今日推荐、最新建议、为什么推荐 | `recommendation_decision` | 基于推荐运行输出解释、风险反驳和行动建议 |
| 单只标的分析、买不买、继续观察吗 | `asset_deep_analysis` | 对单标的做因子、信号、风险、记忆和报告分析 |
| 持仓风险、是否减仓、组合暴露 | `portfolio_monitoring` | 读取持仓快照并输出组合层建议 |
| 观察池维护、是否升级/剔除 | `watchlist_management` | 维护候选池、观察理由和触发条件 |
| 换股、A 和 B 怎么选 | `swap_decision` | 比较两个或多个候选标的 |
| 每日复盘、今天发生了什么 | `daily_review` | 汇总推荐、风险、观察池和记忆变化 |

Hermes 只能选择上表中的 Workflow。未知 Workflow 名称必须视为错误，不允许临时拼接。

## 5. 输出规则

Hermes 给用户的金融回答必须保持固定结构：

1. 结论：说明当前建议动作和置信度边界。
2. 证据：引用 Workflow 报告、因子、信号、风险、数据质量和 Finance Memory。
3. 风险反驳：说明哪些事实会推翻当前建议。
4. 行动：给出观察、等待、记录、确认或复盘等人工动作。
5. 确认边界：明确系统只提供建议和草案，不自动下单。

如果数据不足，必须明确说明缺失维度，并建议先补数据或降级为观察。

## 6. 工具权限边界

Hermes 可见工具必须是只读事实查询或受控 Workflow 调用：

- `portfolio.*`
- `watchlist.*`
- `factor.*`
- `signal_risk.*`
- `memory.*`
- `graph.*`
- `data_quality.*`
- 推荐、报告、Workflow 查询类工具

禁止暴露以下能力：

- 真实下单、撤单、改单。
- 直接访问券商、交易所或外部行情网页。
- 直接修改评分、信号、风险标记。
- 绕过 Workflow 和审计链路写入金融结论。

## 7. MCP 注册参考

MCP Server 推荐命令：

```powershell
D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe -m finance_agent.mcp_server
```

Hermes 或其他 MCP 客户端应注册为：

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

## 8. 交付前检查清单

- 已确认项目路径和 venv Python 存在。
- `workflows list` 能返回 6 个金融团队 Workflow。
- `tools list` 没有交易、下单、执行类写工具。
- 中文报告输出不是乱码。
- MCP 能 initialize 并 list tools。
- 模型不可用时仍有确定性 fallback。
- 回答没有基于外部网页或模型臆测行情。

## 9. 版本同步说明

本文档是仓库内受版本管理的 Skill 副本。同步规则：

- Hermes 侧 skill 修改后，必须同步本文档并记录变更原因。
- 本文档修改后，必须同步 Hermes 侧 `finance-agent-recommendation`。
- 若两边内容冲突，以本文档为准，再由总负责人确认是否更新 Hermes 侧。
