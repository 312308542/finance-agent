# 当前代码架构与 PyCharm 启动指南

本文基于当前代码结构、GitNexus Code Graph 分析结果和本地配置文件整理，用于快速理解 `finance-agent` 的运行边界，并在现有 `.venv`、Docker、数据库和前端工程上用 PyCharm 启动项目。

## 1. 项目定位

`finance-agent` 是一个面向 A 股和数字货币的私人金融 Agent 系统。当前代码已经包含以下核心能力：

- Web 控制台：`apps/agent-office`，负责仪表盘、模型配置、数据同步控制和聊天窗口。
- FastAPI 后端：`src/finance_agent/api`，统一暴露 Dashboard、模型配置、调度器、Workflow、聊天流式接口。
- 数据采集与调度：`src/finance_agent/data`、`src/finance_agent/scheduler` 和 `scripts/data`，负责基础行情、Universe、资金流、事件、衍生品等数据写库。
- Agent 与工具循环：`src/finance_agent/agents`，负责内部金融 Agent、OpenAI-compatible 模型调用、工具协议适配和只读事实工具。
- 指标、因子、筛选、评分、信号和推荐：`src/finance_agent/indicators`、`factors`、`screening`、`scoring`、`signals`、`recommendations`、`pipelines`。
- 存储层：`src/finance_agent/storage`，负责 SQLAlchemy ORM、Repository、迁移和会话管理。
- CLI / MCP：`src/finance_agent/cli`、`src/finance_agent/mcp_server`，为命令行、外部 Agent 和 MCP 工具提供入口。

## 2. 总体架构

```mermaid
flowchart TD
    UI["React Web 控制台\napps/agent-office"] --> API["FastAPI API\nfinance_agent.api.app"]
    CLI["CLI\nfinance-agent"] --> IF["FinanceAgentInterface\nAgent 门面"]
    MCP["MCP Server\nfinance-agent-mcp"] --> IF

    API --> DASH["DashboardService\n应用服务"]
    API --> SYNC["DataSyncControlService\n调度控制"]
    API --> CHAT["FinanceAgentChatSession\n聊天 Agent"]
    API --> MODEL["模型配置 API\nProvider / Model / Route"]

    CHAT --> MC["OpenAICompatibleModelClient\nOpenAI 格式调用"]
    CHAT --> TOOLS["FinanceToolRuntime\n只读事实工具"]
    IF --> TOOLS
    TOOLS --> DB["PostgreSQL + TimescaleDB\n金融事实库"]

    SYNC --> SCHED["BaseDataScheduler\n基础数据调度器"]
    SCHED --> COLLECT["Collectors / Providers\nAKShare / ccxt / Binance"]
    COLLECT --> DB
    SCHED --> ANALYTICS["推荐链路任务\n指标 / 因子 / 评分 / 信号 / 推荐"]
    ANALYTICS --> DB

    MODEL --> DB
    DASH --> DB
    API --> REDIS["Redis\n缓存 / 锁 / 熔断"]
    SCHED --> REDIS

    DB -. 可重建投影 .-> GRAPH["GraphStore\nNeo4j / DozerDB 或 Apache AGE"]
```

## 3. 目录与模块职责

| 路径 | 职责 |
| --- | --- |
| `apps/agent-office` | React + Vite Web 控制台。通过 `VITE_FINANCE_AGENT_API_BASE` 访问后端，默认 `http://127.0.0.1:8000`。 |
| `src/finance_agent/api` | FastAPI 应用入口、路由和依赖注入。`app.py` 创建应用，`routes.py` 挂载 `/api` 下的接口。 |
| `src/finance_agent/application` | 面向 Web API 的应用服务，如 Dashboard 聚合、数据同步控制、调度器进程管理。 |
| `src/finance_agent/agents` | 内部金融 Agent、聊天会话、Workflow 门面、模型运行时、工具运行时。 |
| `src/finance_agent/data` | 数据源 Provider、采集器、同步配置、字段清洗和归一化逻辑。 |
| `src/finance_agent/scheduler` | 基础数据调度器，把配置文件中的 jobs 编排成采集和分析任务。 |
| `src/finance_agent/indicators` | 技术指标计算服务。 |
| `src/finance_agent/factors` | 因子计算、归一化和证据生成。 |
| `src/finance_agent/screening` | 候选池初筛规则。 |
| `src/finance_agent/scoring` | 候选资产评分。 |
| `src/finance_agent/signals` | 信号快照生成。 |
| `src/finance_agent/recommendations` | 推荐结果、推荐运行和推荐条目服务。 |
| `src/finance_agent/pipelines` | 组合式推荐流水线，当前核心是 `UniverseRecommendationPipeline`。 |
| `src/finance_agent/storage` | SQLAlchemy ORM、Repository、数据库连接和 Alembic 迁移。 |
| `src/finance_agent/graph` | Finance Memory 图谱投影和图查询能力。 |
| `src/finance_agent/cli` | 命令行入口，安装后暴露 `finance-agent`。 |
| `src/finance_agent/mcp_server` | MCP Server 入口，安装后暴露 `finance-agent-mcp`。 |
| `scripts/data` | 数据采集、调度、健康检查和 smoke 脚本。 |
| `scripts/storage` | 存储、CLI、Agent、模型工具循环等 smoke 验证脚本。 |
| `runtime` | 本地运行时配置、调度状态、事件日志和进程日志。 |

## 4. 关键运行链路

### 4.1 Web 控制台到后端

前端从 `apps/agent-office/src/api.ts` 读取 API Base：

```text
VITE_FINANCE_AGENT_API_BASE || http://127.0.0.1:8000
```

后端入口是 `finance_agent.api.app:app`。`create_app()` 创建 FastAPI 应用，挂载 CORS，并把 `routes.py` 中的 router 统一放到 `/api` 前缀下。

典型链路：

```text
React 页面 -> /api/... -> routes.py -> application service / FinanceAgentInterface -> Repository -> PostgreSQL
```

### 4.2 聊天 Agent 与流式输出

聊天接口在 `/api/chat/stream`。当前设计不是前端伪造 Agent 结果，而是：

```text
Web 聊天窗口
-> POST /api/chat/stream
-> FinanceAgentChatSession
-> 选择可用模型配置
-> OpenAI-compatible Chat Completions
-> 模型自主发起 tool_calls
-> FinanceToolRuntime 调用只读事实工具
-> 工具结果回填模型
-> SSE 推送 status / model_call / tool_call / tool_result / delta / done
```

需要注意：

- 模型配置必须在数据库里 ready，否则聊天会返回模型不可用说明。
- 工具调用遵循 OpenAI Chat Completions 的 `tools` / `tool_calls` / `role=tool` 消息结构。
- 工具只能读取已入库事实，不允许模型直接访问 AKShare、Binance 或数据库连接。
- 前端打字机效果属于展示层；真正的流式事件来自 `/api/chat/stream`。

### 4.3 模型配置与路由

模型配置由数据库驱动，主要包含：

- Provider：供应商、OpenAI-compatible `base_url`、`api_key`、启用状态。
- Model Instance：模型 key、模型名称、类型、角色、所属 Provider。
- Model Route：按角色、任务、Workflow 类型、高风险场景选择模型。

后端相关文件：

- `src/finance_agent/agents/runtime/model_config.py`
- `src/finance_agent/agents/runtime/model_client.py`
- `src/finance_agent/storage/repositories.py`
- `src/finance_agent/api/routes.py`

Web 页面中的连通性测试会通过后端发起一次 OpenAI-compatible 请求，用来确认 `base_url`、`api_key` 和模型名称是否可用。

### 4.4 基础数据调度器

调度器配置来源通常是：

- `runtime/data_sync_config.json`
- `runtime/base_data_scheduler/base_data_scheduler.json`

Web 页面可以通过 `/api/data/scheduler/start` 启动独立调度器进程。命令行入口是：

```powershell
.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\base_data_scheduler.json --loop --status-file runtime\base_data_scheduler\status.json --event-log-file runtime\base_data_scheduler\events.jsonl
```

调度器运行链路：

```text
BaseDataScheduler
-> 读取 scheduler jobs
-> 按 interval_seconds 判断是否执行
-> 调用 collect_base_data 参数构建
-> Collectors / Providers 拉取数据
-> 清洗归一化
-> Repository 写入资产身份、资产详情附表、标准事实表和 raw_records
-> 可选执行 analytics jobs
```

常见运行时文件：

| 文件 | 作用 |
| --- | --- |
| `runtime/data_sync_config.json` | 数据同步向导保存的高层配置。 |
| `runtime/base_data_scheduler/base_data_scheduler.json` | 调度器可执行 jobs 配置。 |
| `runtime/base_data_scheduler/status.json` | 调度器健康状态和最后任务状态。 |
| `runtime/base_data_scheduler/events.jsonl` | 结构化任务事件日志。 |
| `runtime/base_data_scheduler/process.log` | 子进程 stdout / stderr 日志。 |

`status=stale` 通常表示 `status.json` 超过阈值未刷新，不等同于没有写库。排查时应同时看 `process.log`、`events.jsonl`、数据库表行数和 Provider 熔断状态。

### 4.5 资产主表与详情附表

资产层现在采用“稳定主表 + 多附表”的写库设计，避免多个采集任务并发更新 `assets` 同一行导致 PostgreSQL deadlock。

`assets` 只作为资产身份主表，保存稳定字段：

- `asset_id`
- `symbol`
- `market`
- `asset_type`
- `exchange`
- `currency`
- `base_asset`
- `quote_asset`

容易频繁变化或由不同 Provider 补全的数据进入附表：

| 表 | 用途 |
| --- | --- |
| `asset_profiles` | 名称、行业、概念、来源侧慢变资料。 |
| `asset_provider_mappings` | AKShare、ccxt、Binance 等 Provider 的代码映射。 |
| `asset_status_snapshots` | 可交易、停复牌、退市等交易状态快照。 |
| `realtime_quote_snapshots` | 最新价、涨跌、成交量、成交额、换手率等实时行情快照。 |
| `market_bars` | K 线事实表。 |
| `fundamental_snapshots` | 财务和估值快照。 |
| `capital_flow_snapshots` | 资金流快照。 |
| `event_records` / `risk_findings` | 新闻公告、风险和情绪事件。 |

采集器在高频路径中使用 `AssetRepository.ensure_asset()` 写 `assets`，底层是 `INSERT ... ON CONFLICT DO NOTHING`。这意味着并发任务只在资产不存在时创建主表行，不再反复 `DO UPDATE` 同一资产行；动态数据则各自写入附表或事实表，降低死锁概率。

### 4.6 推荐分析流水线

推荐链路以同市场候选池为边界，不能把 A 股和数字货币混进同一个 Universe：

```text
asset_universes / asset_universe_members
-> market_bars / fundamentals / capital_flow / events / risk / derivatives
-> IndicatorService
-> FactorService
-> ScreeningService
-> ScoringService
-> SignalService
-> UniverseRecommendationPipeline
-> recommendation_runs / asset_recommendations
```

核心入口：

- `src/finance_agent/pipelines/recommendation.py`
- `src/finance_agent/indicators`
- `src/finance_agent/factors`
- `src/finance_agent/screening`
- `src/finance_agent/scoring`
- `src/finance_agent/signals`
- `src/finance_agent/recommendations`

如果聊天回答提示推荐数据来源是 `smoke`，说明推荐表中当前可见的推荐运行来自 smoke 脚本或样例数据，不应被当作实时买入建议。需要让基础采集和推荐流水线在真实配置下完成写库。

### 4.7 存储与迁移

当前事实源是 PostgreSQL + TimescaleDB。Redis 只做缓存、锁、Provider 熔断和短期上下文，不保存唯一金融事实。

主要环境变量：

```text
FINANCE_AGENT_DATABASE_URL=postgresql+psycopg://finance_agent:finance_agent@localhost:5432/finance_agent
FINANCE_AGENT_REDIS_URL=redis://localhost:6379/0
FINANCE_AGENT_GRAPH_CONFIG_FILE=config/graph.neo4j.example.toml
```

数据库迁移由 Alembic 管理：

```powershell
.venv\Scripts\alembic.exe upgrade head
```

也可以使用：

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
```

## 5. 使用现有环境在 PyCharm 启动

### 5.1 打开项目

在 PyCharm 中打开：

```text
D:\Code\aiAgents\finance-agent
```

Python Interpreter 选择已有虚拟环境：

```text
D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe
```

如果 PyCharm 没有自动识别 `src` 布局，有两种处理方式：

- 推荐：在 PyCharm 的 Project 视图中右键 `src`，选择 `Mark Directory as -> Sources Root`。
- 或者在虚拟环境中安装 editable 包：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 5.2 准备基础设施

在 PyCharm Terminal 或系统 PowerShell 中启动 PostgreSQL + Redis：

```powershell
docker compose up -d postgres redis
```

确认容器启动后执行迁移：

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
```

如果本地 `.env` 已经存在，PyCharm 可以通过 EnvFile 插件加载它；没有插件时，在 Run Configuration 的 Environment variables 中至少配置：

```text
FINANCE_AGENT_DATABASE_URL=postgresql+psycopg://finance_agent:finance_agent@localhost:5432/finance_agent
FINANCE_AGENT_REDIS_URL=redis://localhost:6379/0
FINANCE_AGENT_GRAPH_CONFIG_FILE=config/graph.neo4j.example.toml
```

### 5.3 后端 Run Configuration

新建 `Python` 类型配置：

| 配置项 | 值 |
| --- | --- |
| Name | `finance-agent-api` |
| Run | `Module name` |
| Module name | `uvicorn` |
| Parameters | `finance_agent.api.app:app --host 127.0.0.1 --port 8000 --reload` |
| Working directory | `D:\Code\aiAgents\finance-agent` |
| Python interpreter | `D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe` |
| Environment variables | 使用 `.env`，或手动设置数据库和 Redis 变量 |

等价命令：

```powershell
.venv\Scripts\python.exe -m uvicorn finance_agent.api.app:app --host 127.0.0.1 --port 8000 --reload
```

启动后访问：

```text
http://127.0.0.1:8000/docs
```

### 5.4 前端 Run Configuration

新建 `npm` 类型配置：

| 配置项 | 值 |
| --- | --- |
| Name | `agent-office-dev` |
| package.json | `D:\Code\aiAgents\finance-agent\apps\agent-office\package.json` |
| Command | `run` |
| Scripts | `dev` |
| Working directory | `D:\Code\aiAgents\finance-agent\apps\agent-office` |
| Environment variables | `VITE_FINANCE_AGENT_API_BASE=http://127.0.0.1:8000` |

等价命令：

```powershell
cd apps\agent-office
$env:VITE_FINANCE_AGENT_API_BASE = "http://127.0.0.1:8000"
npm run dev
```

Vite 会打印实际访问地址。当前本机曾使用过：

```text
http://127.0.0.1:5177/
```

如果 5173 被占用，Vite 自动换端口是正常现象。

### 5.5 调度器 Run Configuration

如果希望从 PyCharm 直接启动真实调度器，新建 `Python` 类型配置：

| 配置项 | 值 |
| --- | --- |
| Name | `base-data-scheduler` |
| Script path | `D:\Code\aiAgents\finance-agent\scripts\data\run_base_data_scheduler.py` |
| Parameters | `--config runtime\base_data_scheduler\base_data_scheduler.json --loop --status-file runtime\base_data_scheduler\status.json --event-log-file runtime\base_data_scheduler\events.jsonl` |
| Working directory | `D:\Code\aiAgents\finance-agent` |
| Python interpreter | `D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe` |
| Environment variables | 使用和后端相同的数据库、Redis、图谱配置 |

验证计划但不写库：

```powershell
.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\base_data_scheduler.json --loop --dry-run --max-cycles 1
```

真实执行一轮：

```powershell
.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\base_data_scheduler.json --run-once
```

Web 控制台也可以通过 `/api/data/scheduler/start` 启动调度器。两种方式不要同时启动同一套配置，避免任务锁和日志判断混乱。

## 6. 常用验证命令

后端依赖安装：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

数据库迁移：

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
```

基础数据健康检查：

```powershell
.venv\Scripts\python.exe scripts\data\check_base_data_health.py --cache-backend redis
```

查看调度器健康：

```powershell
.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --health-check --status-file runtime\base_data_scheduler\status.json
```

前端构建：

```powershell
cd apps\agent-office
npm run build
```

模型配置页面连通性验证可以直接使用 Web 控制台中的「测试连通性」按钮，也可以调用后端 `/api/models/providers/test-connectivity`。

## 7. 常见问题排查

### 后端启动提示找不到 `finance_agent`

优先检查：

- PyCharm Working directory 是否是 `D:\Code\aiAgents\finance-agent`。
- Interpreter 是否是 `.venv\Scripts\python.exe`。
- 是否执行过 `.venv\Scripts\python.exe -m pip install -e ".[dev]"`。
- `src` 是否被标记为 Sources Root。

### 数据库连接失败

检查：

- `docker compose ps` 中 `postgres` 和 `redis` 是否 healthy。
- `FINANCE_AGENT_DATABASE_URL` 是否指向 `localhost:5432/finance_agent`。
- 是否执行过 `alembic upgrade head`。
- 本机 5432 或 6379 是否被其他服务占用。

### 前端请求 API 失败

检查：

- 后端是否启动在 `http://127.0.0.1:8000`。
- 前端环境变量 `VITE_FINANCE_AGENT_API_BASE` 是否正确。
- 浏览器控制台 Network 中请求是否打到了 `/api/...`。
- 后端 CORS 当前允许所有 origin，通常不是跨域配置问题。

### 调度器显示 `stale`

`stale` 表示状态文件超过 `health_stale_seconds` 未刷新。它可能由以下原因导致：

- 调度器已经完成一轮，状态文件自然不再刷新。
- 正在等待外部数据源响应，AKShare、东方财富或 Binance 请求超时。
- Provider 熔断或任务锁让部分任务被跳过。
- 进程还在，但 stdout / stderr 中有异常，需要看 `process.log`。

建议同时查看：

```text
runtime/base_data_scheduler/status.json
runtime/base_data_scheduler/events.jsonl
runtime/base_data_scheduler/process.log
```

### 模型聊天没有真正调用 LLM

检查：

- 模型供应商是否已写入数据库并启用。
- 每个模型实例是否有一对一的 `base_url` 和 `api_key`。
- 模型连通性测试是否成功。
- `/api/chat/stream` 事件里是否出现 `model_call`、`model_result`、`tool_call`。
- 如果返回模型不可用，需要先配置 ready 的 `primary_financial_analyst` 或可作为主分析模型的实例。

### 回答提示数据是 `smoke`

说明当前推荐表里可见的推荐运行来自 smoke / 样例数据。需要启动真实基础数据采集和推荐流水线，并确认推荐运行写入的来源不是 smoke。排查重点是：

- `asset_universes` 和 `asset_universe_members` 是否有真实候选池。
- `market_bars`、`fundamental_snapshots`、`capital_flow_snapshots`、`event_records`、`risk_findings` 是否持续增长。
- `recommendation_runs` 和 `asset_recommendations` 是否有新 run。
- 调度器 events 中 analytics jobs 是否成功完成。

## 8. 推荐启动顺序

日常开发时建议按这个顺序启动：

1. `docker compose up -d postgres redis`
2. `.venv\Scripts\python.exe -m alembic upgrade head`
3. PyCharm 启动 `finance-agent-api`
4. PyCharm 启动 `agent-office-dev`
5. 在 Web 控制台测试模型连通性
6. 需要数据更新时，通过 Web 控制台或 PyCharm 启动 `base-data-scheduler`

这样启动后，前端、后端、数据库、Redis、模型配置和调度器日志都在本机现有环境内闭环，问题也比较容易定位。

## 9. 2026-05-30 数据维护更新

- `raw_records` 已增加精确去重唯一索引 `uq_raw_records_exact_dedup`，相同 Provider、接口、请求哈希、内容哈希和状态只保留一条 canonical 原始响应。
- `fundamental_snapshots` 新增按 `asset_id + source + as_of/report_period` 的查询索引和唯一约束；财务指标来源与日频估值来源在因子计算中分开读取。
- `UniverseRecommendationPipeline` 先检查指标覆盖率，再计算因子；覆盖率不足时不写下游因子、筛选、评分、信号或推荐快照。
- 当前 `runtime/base_data_scheduler/base_data_scheduler.json` 已重新导出，analytics jobs 会携带 `min_bars`、`min_indicator_coverage_ratio`、`min_factor_coverage_ratio` 和 `min_available_factor_groups`。
