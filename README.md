# Finance Agent

面向 A 股与数字货币的私人金融 Agent 系统。它把数据同步、因子与信号计算、推荐流水线、金融记忆、模型路由、Web 控制台、CLI 和 MCP 工具统一在一个可审计的本地工作台里，目标是帮助个人投资者持续追踪标的、复盘决策、理解风险，并生成有证据链的中文分析建议。

> 本项目不提供投资建议、收益承诺或自动交易能力。所有买入、卖出、加仓、减仓、换仓等输出都应视为研究与辅助决策材料，真实交易必须由用户自行判断并在外部交易系统中确认。

## 愿景

Finance Agent 希望成为一个“会记账、会复盘、会解释”的私人金融研究助理，而不是黑箱荐股工具。系统关注三件事：

- 把 A 股和数字货币的行情、基本面、资金流、衍生品、事件与风险数据稳定入库。
- 用透明规则和可回放流水线生成因子、信号、评分、推荐与风险反驳。
- 让 Agent 只能基于已入库事实、历史记忆和审计日志进行解释，避免凭空编造行情或指标。

## 核心能力

- **多市场数据层**：面向 A 股、Binance 现货和合约市场，支持 Universe、K 线、实时快照、财务估值、资金流、事件、风险和衍生品数据入库。
- **推荐与风控链路**：通过指标、因子、筛选、评分、信号和推荐流水线，把候选池转化为可解释的推荐、观察和回避列表。
- **金融 Agent 工作流**：通过固定金融团队 Workflow 生成报告、风险反驳、换仓比较和决策审计。
- **Finance Memory**：记录候选入池原因、每日关注理由、用户反馈、复盘结论和历史决策路径，可投影到图数据库。
- **Web 控制台**：提供仪表盘、模型配置、数据同步控制、任务监控、中文报告、推荐决策、持仓监控、风险中心、Agent 运行、Finance Memory 和聊天窗口。
- **CLI / MCP 接口**：为本地自动化、外部 Agent 和 MCP 客户端暴露工作流、只读事实工具、图谱、记忆和触发事件能力。

## 架构概览

```mermaid
flowchart TD
    UI["React Web 控制台\napps/agent-office"] --> API["FastAPI API\nsrc/finance_agent/api"]
    CLI["CLI\nfinance-agent"] --> IF["FinanceAgentInterface"]
    MCP["MCP Server\nfinance-agent-mcp"] --> IF

    API --> DASH["DashboardService"]
    API --> SYNC["DataSyncControlService"]
    API --> CHAT["FinanceAgentChatSession"]
    API --> MODEL["模型配置与路由"]

    CHAT --> LLM["OpenAI-compatible 模型"]
    CHAT --> TOOLS["FinanceToolRuntime\n只读事实工具"]
    IF --> TOOLS
    IF --> WF["金融团队 Workflow"]

    SYNC --> SCHED["BaseDataScheduler"]
    SCHED --> COLLECT["Collectors / Providers\nAKShare / ccxt / Binance"]
    COLLECT --> DB["PostgreSQL + TimescaleDB"]
    TOOLS --> DB
    WF --> DB
    DASH --> DB
    MODEL --> DB

    DB --> PIPE["指标 / 因子 / 筛选 / 评分 / 信号 / 推荐"]
    PIPE --> DB
    DB -.可重建投影.-> GRAPH["GraphStore\nNeo4j / DozerDB / Apache AGE"]
```

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `src/finance_agent/api` | FastAPI 应用、路由和请求响应模型。 |
| `src/finance_agent/application` | Web API 复用的应用服务，如 Dashboard 聚合和数据同步控制。 |
| `src/finance_agent/agents` | 金融 Agent、聊天会话、模型运行时、工具运行时和 Workflow 门面。 |
| `src/finance_agent/data` | 数据源 Provider、采集器、同步配置和数据归一化逻辑。 |
| `src/finance_agent/scheduler` | 基础数据调度器和任务编排。 |
| `src/finance_agent/indicators` | 技术指标计算。 |
| `src/finance_agent/factors` | 因子计算、归一化和证据生成。 |
| `src/finance_agent/screening` | 候选池筛选规则。 |
| `src/finance_agent/scoring` | 多维评分服务。 |
| `src/finance_agent/signals` | 信号快照生成。 |
| `src/finance_agent/recommendations` | 推荐运行和推荐条目服务。 |
| `src/finance_agent/pipelines` | 组合式推荐流水线。 |
| `src/finance_agent/storage` | SQLAlchemy ORM、Repository、数据库连接和 Alembic 迁移。 |
| `src/finance_agent/graph` | Finance Memory 图谱投影和查询。 |
| `src/finance_agent/cli` | 命令行入口。 |
| `src/finance_agent/mcp_server` | MCP Server 入口。 |
| `apps/agent-office` | React + Vite Web 控制台。 |
| `scripts/data` | 数据采集、调度和健康检查脚本。 |
| `scripts/storage` | 存储、API、Agent、模型和工具链路 smoke 脚本。 |
| `docs` | 架构、数据库、模型、调度器、Workflow 和路线记录。 |

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 20+
- Docker 与 Docker Compose
- Windows PowerShell 或兼容 Shell
- TA-Lib 运行依赖。Windows 环境如果安装失败，需要先准备对应的本地二进制依赖。

### 1. 克隆仓库

```powershell
git clone https://github.com/312308542/finance-agent.git
cd finance-agent
```

### 2. 启动基础服务

```powershell
docker compose up -d postgres redis
```

默认会启动：

- PostgreSQL + TimescaleDB：`localhost:5432`
- Redis：`localhost:6379`

### 3. 安装后端依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 4. 配置环境变量

```powershell
Copy-Item .env.example .env
```

默认开发配置使用：

```text
FINANCE_AGENT_DATABASE_URL=postgresql+psycopg://finance_agent:finance_agent@localhost:5432/finance_agent
FINANCE_AGENT_REDIS_URL=redis://localhost:6379/0
FINANCE_AGENT_GRAPH_CONFIG_FILE=config/graph.neo4j.example.toml
```

### 5. 初始化数据库

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

### 6. 启动 API

```powershell
.\.venv\Scripts\python.exe -m uvicorn finance_agent.api.app:app --app-dir src --host 127.0.0.1 --port 8000
```

健康检查：

```text
http://127.0.0.1:8000/api/health
```

### 7. 启动 Web 控制台

```powershell
cd apps/agent-office
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

浏览器打开：

```text
http://127.0.0.1:5173
```

## 常用命令

### CLI

安装为 editable 包后，可以使用 `finance-agent` 命令：

```powershell
finance-agent workflows list
finance-agent tools list
finance-agent models config
finance-agent chat --owner-id owner:demo --message "查看当前能力"
```

运行 Workflow 示例：

```powershell
finance-agent workflows run asset_deep_analysis `
  --owner-id owner:demo `
  --asset-id asset:demo:600519 `
  --portfolio-id portfolio:demo `
  --watchlist-id watchlist:demo
```

读取报告：

```powershell
finance-agent reports show workflow:demo:asset_deep_analysis:20260518160000 --markdown
```

### 数据同步配置

生成数据同步配置：

```powershell
finance-agent data config init `
  --preset personal-comprehensive `
  --markets ashare,crypto_spot,crypto_future `
  --output runtime\data_sync_config.json
```

预览并导出调度器计划：

```powershell
finance-agent data config preview --config-file runtime\data_sync_config.json
finance-agent data config export `
  --config-file runtime\data_sync_config.json `
  --output runtime\base_data_scheduler\base_data_scheduler.json
```

运行一次调度：

```powershell
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py `
  --config runtime\base_data_scheduler\base_data_scheduler.json `
  --run-once
```

常驻调度：

```powershell
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py `
  --config runtime\base_data_scheduler\base_data_scheduler.json `
  --loop `
  --status-file runtime\base_data_scheduler\status.json `
  --event-log-file runtime\base_data_scheduler\events.jsonl
```

### MCP Server

```powershell
finance-agent-mcp
```

MCP 暴露的能力包括 Workflow 调度、只读事实工具、触发事件、图谱同步、图谱追踪和 Finance Memory 召回。

## Web 控制台能力

当前控制台位于 `apps/agent-office`，默认访问后端 `http://127.0.0.1:8000`。可以通过 `VITE_FINANCE_AGENT_API_BASE` 覆盖：

```powershell
$env:VITE_FINANCE_AGENT_API_BASE="http://127.0.0.1:8000"
```

主要页面能力：

- Dashboard 总览
- 报告、推荐、持仓、风险、Agent 运行和 Finance Memory 页面已接真实后端只读接口
- 中文报告列表与 Markdown 详情展示，支持从推荐项跳转到关联 Workflow 报告
- 推荐结果、风险反驳和待确认决策反馈入口；推荐页反馈会调用决策反馈 API，真实落库验收留到统一联调阶段
- 持仓集中度、风险发现、Workflow 审计和记忆时间线展示
- 数据同步配置与调度器控制
- 模型供应商、模型实例和路由配置
- 支持 SSE 的聊天窗口

## 模型配置

系统使用 OpenAI-compatible Chat Completions 接口接入模型。模型配置可以通过 CLI 或 Web 控制台写入数据库。

CLI 示例：

```powershell
finance-agent models init `
  --deepseek-base-url https://api.deepseek.com/v1 `
  --deepseek-api-key "<your-api-key>"

finance-agent models list
finance-agent models route-preview
```

模型只能通过白名单只读工具读取已入库事实，不应直接联网抓取行情或绕过数据层生成指标。

## 开发与验证

后端测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

前端构建：

```powershell
cd apps/agent-office
npm run build
```

前端脚本测试：

```powershell
npm run test:agent-routes
npm run test:api-connectivity
npm run test:model-config
npm run test:data-sync
npm run test:chat-stream
```

常用 smoke 脚本：

```powershell
.\.venv\Scripts\python.exe scripts\storage\smoke_dashboard_api.py
.\.venv\Scripts\python.exe scripts\storage\smoke_data_sync_control_service.py
.\.venv\Scripts\python.exe scripts\storage\smoke_agent_chat_model_tool_loop.py
```

## 路线图

### 近期

- 完善数据同步配置向导，降低 A 股和数字货币基础池初始化成本。
- 增强调度器可观测性，补全任务事件、健康检查和失败重试视图。
- 继续完善 Web 控制台的人工确认、操作草案、执行登记和复盘闭环。
- 强化模型配置体验，支持更多 OpenAI-compatible 供应商和路由预览场景。

### 中期

- 扩展 Finance Memory 图谱查询，支持更完整的入池原因链、历史相似决策和风险传导分析。
- 完善推荐流水线的因子有效性评估、回测摘要和多市场分层排名。
- 增加更多只读事实工具，让 Agent 回答能更稳定地引用入库证据。
- 提供更标准的部署文档和生产配置模板。

### 长期

- 构建更完整的个人投资复盘闭环：建议、确认、执行记录、反馈、复盘和记忆更新。
- 引入可验证的量化模型实验框架，但保持 LLM 只负责解释、比较和反驳。
- 在严格人工确认和权限控制下，探索订单草稿与交易系统适配。

## 设计原则

- **事实优先**：行情、财务、资金流、事件、风险和推荐结果必须来自已入库数据。
- **可审计**：Workflow、工具调用、模型路由、用户反馈和复盘都应留下结构化记录。
- **不自动交易**：系统可以生成建议和订单草稿，但不绕过用户确认执行真实交易。
- **中文可解释**：面向金融新手时，必须把指标、风险和证据翻译成可理解的中文说明。
- **模型可替换**：业务逻辑不绑定具体模型，模型通过 Provider、Instance 和 Route 配置进入系统。

## 文档索引

- [项目计划](docs/项目计划.md)
- [架构方案](docs/架构方案.md)
- [当前代码架构与 PyCharm 启动指南](docs/当前代码架构与PyCharm启动指南.md)
- [数据库设计](docs/数据库设计.md)
- [数据库启动与迁移](docs/数据库启动与迁移.md)
- [基础数据调度器](docs/基础数据调度器.md)
- [CLI 与 MCP 工具入口](docs/CLI与MCP工具入口.md)
- [模型选型与职责分配](docs/模型选型与职责分配.md)
- [Workflow 层设计](docs/Workflow层设计.md)

## 贡献

欢迎围绕以下方向提交 Issue 或 Pull Request：

- 数据源适配和字段归一化
- 调度器稳定性和可观测性
- 因子、信号、评分和推荐解释
- Web 控制台体验
- CLI / MCP 工具可用性
- 文档、示例和部署流程

提交前建议运行：

```powershell
.\.venv\Scripts\python.exe -m pytest
cd apps/agent-office
npm run build
```

## 安全与合规

- 不要提交 `.env`、API Key、数据库密码、运行日志、截图或 `runtime/` 数据。
- 测试用密钥请使用明显的占位字符串，例如 `test-api-key-*`。
- 项目输出不构成投资建议，用户应自行承担投资风险。
- 如果接入真实账户或交易系统，必须增加显式权限边界、二次确认和审计记录。

## 许可证

本项目使用 [GNU General Public License v3.0](LICENSE) 开源。
