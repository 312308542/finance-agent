# Web 控制台与 FastAPI 重构实施计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 或等价的计划执行流程。步骤使用复选框语法跟踪进度。

**目标：** 把当前办公室动画/演示控制台重构为可接真实后端的私人金融助手 Web 控制台，并新增 FastAPI Dashboard API。

**架构：** 后端新增薄 API 层和 Dashboard 聚合服务，复用 `FinanceAgentInterface`、仓储、Workflow、Memory 和模型配置；前端保持 React + Vite，改为数据密集型金融运营终端。

**技术栈：** FastAPI、SQLAlchemy、React、Vite、TypeScript、lucide-react。

---

## 文件结构

- 创建 `src/finance_agent/api/__init__.py`：暴露 `create_app`。
- 创建 `src/finance_agent/api/app.py`：FastAPI 应用和路由注册。
- 创建 `src/finance_agent/api/deps.py`：数据库会话依赖。
- 创建 `src/finance_agent/api/schemas.py`：Web API 请求/响应模型。
- 创建 `src/finance_agent/api/routes.py`：Dashboard API 路由。
- 创建 `src/finance_agent/application/dashboard_service.py`：Dashboard 聚合查询。
- 保持 `src/finance_agent/application/__init__.py` 不导出 Dashboard 服务，避免引入 `FinanceAgentInterface` 循环依赖。
- 修改 `pyproject.toml`：加入 FastAPI 与 uvicorn。
- 创建 `scripts/storage/smoke_dashboard_api.py`：API 冒烟验证。
- 重写 `apps/agent-office/src/main.tsx`：控制台主界面。
- 重写 `apps/agent-office/src/styles.css`：金融工作台视觉。
- 创建 `apps/agent-office/src/api.ts`：前端 API client 和降级数据。
- 删除 `apps/agent-office/src/ParticleField.tsx`、`consoleData.ts`、`data/officeData.tsx` 和 `game/` 旧办公室实现。
- 修改 `apps/agent-office/README.md`、`package.json`、`package-lock.json` 和 `index.html`，切换为 Finance Agent 控制台命名并移除 Phaser 依赖。
- 扩展 `src/finance_agent/api/routes.py` 和 `schemas.py`：新增模型供应商、模型实例、Agent 路由保存接口，以及模型路由预览接口。

## 任务 1：后端 API 合约和冒烟脚本

- [x] 创建 API smoke 脚本，使用 FastAPI `TestClient` 调用 `GET /api/health`、`GET /api/dashboard/summary`、`GET /api/workflows`。
- [x] 首次运行应因为 `finance_agent.api.app` 不存在而失败。
- [x] 失败输出应指向缺失 API 模块。

## 任务 2：FastAPI 应用和 Dashboard 聚合服务

- [x] 新增 FastAPI 依赖。
- [x] 新增 `DashboardService`，聚合组合、观察池、推荐、风险、Workflow、数据质量、模型配置。
- [x] 新增 `create_app()` 和路由。
- [x] 运行 API smoke，确认接口返回 `ok`、`empty`、`partial` 或 `unavailable`，不能抛出未处理异常。

## 任务 3：前端控制台重构

- [x] 建立前端 API client，支持 `VITE_FINANCE_AGENT_API_BASE`。
- [x] API 失败时返回本地演示快照，并标记 `source=fallback`。
- [x] 重写主界面为左侧导航、顶部状态栏、核心数据区、右侧 Agent 上下文、底部事件流。
- [x] 不再渲染办公室动画或旧粒子主视觉。
- [x] 删除旧 Phaser 办公室实现和 `phaser` 依赖。

## 任务 4：验证和收尾

- [x] 运行 Python compileall。
- [x] 运行本轮新增范围局部 ruff。
- [x] 运行 API smoke。
- [x] 运行前端 build。
- [x] 使用浏览器检查桌面和移动宽度的控制台页面。
- [x] 检查 `git status --short`，确认改动范围与本计划一致。

## 任务 5：模型配置闭环

- [x] 后端新增 `PUT /api/models/providers/{provider_key}`，写入 `model_providers`。
- [x] 后端新增 `PUT /api/models/instances/{model_key}`，写入 `model_instances`。
- [x] 后端新增 `PUT /api/models/routes/{role}`，写入 `model_routing_rules`。
- [x] 后端新增 `GET /api/models/routes/preview`，复用 `ModelRoutingPolicy` 预览 Agent 实际路由。
- [x] 供应商保存时 `api_key` 不传则保留旧密钥，避免页面留空误清空。
- [x] 前端模型配置页支持保存供应商、保存模型实例、切换主分析 Agent 和高风险复核 Agent 模型。
- [x] 前端保存后刷新 Dashboard 摘要和路由预览，页面展示 `ready/not-ready`。
- [x] 浏览器端到端验证：页面新增 `web-e2e-provider`、`web-e2e-primary`、`web-e2e-review` 后，路由预览显示 `primary_financial_analyst -> web-e2e-primary / ready`、`high_risk_reviewer -> web-e2e-review / ready`。

## 验证记录

- `.venv\Scripts\python.exe -m compileall src scripts`：通过。
- `.venv\Scripts\ruff.exe check src\finance_agent\api src\finance_agent\application\dashboard_service.py scripts\storage\smoke_dashboard_api.py`：通过。
- `.venv\Scripts\python.exe scripts\storage\smoke_dashboard_api.py`：通过，输出 `Dashboard API smoke passed`。
- `npm run build`，工作目录 `apps/agent-office`：通过。
- 浏览器检查 `http://127.0.0.1:5173`：桌面和移动宽度均可打开，标题为 `Finance Agent 控制台`，没有办公室动画和浏览器错误日志。
- 浏览器检查 `http://127.0.0.1:5173` 的模型配置页：供应商、模型实例和 Agent 路由均可保存；路由预览最终展示新模型且浏览器日志无 error/warning。

全仓 `ruff check src scripts` 当前仍失败，原因是历史文件中的导入排序、长行和异常链问题；本轮没有把这些历史债务混入 Web 控制台重构。
