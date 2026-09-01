# Finance Agent 控制台

这是私人金融助手的 Web 控制台前端，定位是数据密集型金融运营终端，而不是动画演示页。页面用于查看持仓监控、观察池、推荐决策、风险反驳、Workflow 审计、Finance Memory、数据同步和模型配置。

## 当前边界

- 前端只展示可审计摘要、结构化事实、工具调用结果、风险提示和中文报告入口。
- 不展示模型隐藏推理链。
- 买入、卖出、加仓、减仓、换股等动作只展示建议和证据，不自动执行真实交易。
- 数据来自后端 FastAPI，接口不可用时使用本地降级示例数据，方便前端独立预览。

## 目录

```text
src/
  api.ts        # Web 控制台 API 客户端和降级数据
  main.tsx     # React 控制台页面
  styles.css   # 金融终端样式
```

## 后端接口

默认读取：

```text
http://127.0.0.1:8000
```

可通过环境变量覆盖：

```powershell
$env:VITE_FINANCE_AGENT_API_BASE="http://127.0.0.1:8000"
```

当前页面消费的主要接口：

- `GET /api/dashboard/summary`
- `GET /api/portfolio/overview`
- `GET /api/watchlists`
- `GET /api/recommendations/latest`
- `GET /api/risks`
- `GET /api/workflows`
- `GET /api/models/config`
- `GET /api/data/scheduler/status`
- `POST /api/chat`

## Docker 运行

在仓库根目录构建并启动完整系统：

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

浏览器访问 `http://127.0.0.1:5173`。生产前端由 Nginx 提供静态资源，并将同源 `/api` 请求转发到 Compose 内的 `finance-agent-api:8000`。

## 源码开发

先启动后端：

```powershell
cd D:\Code\aiAgents\finance-agent
.venv\Scripts\python.exe -m uvicorn finance_agent.api.app:app --app-dir src --host 127.0.0.1 --port 8000
```

再启动前端：

```powershell
cd D:\Code\aiAgents\finance-agent\apps\agent-office
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

## 验证

```powershell
cd D:\Code\aiAgents\finance-agent
.venv\Scripts\python.exe scripts\storage\smoke_dashboard_api.py

cd D:\Code\aiAgents\finance-agent\apps\agent-office
npm run build
```

## 后续演进

- 增加 SSE 或 WebSocket，实时推送 Agent 状态、Workflow 节点事件、风险触发和数据同步状态。
- 将推荐、观察池、风险反驳和报告页面拆成可路由的详情页。
- 接入图谱记忆和向量检索详情视图，让候选池纳入原因、每日关注原因和复盘结论可追溯。
- 增加模型供应商、模型实例、路由规则和检索配置的在线编辑能力。
