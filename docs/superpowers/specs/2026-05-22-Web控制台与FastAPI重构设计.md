# Web 控制台与 FastAPI 重构设计

## 1. 背景

用户已经明确否定“办公室动画”和偏演示性的粒子页，当前 Web 端需要回到私人金融助手的核心目标：监控持仓、管理观察池/候选池、展示 Agent 决策、风险反驳、中文报告、数据同步健康和模型配置。

本设计使用 `ui-ux-pro-max` 重新检索并采纳以下结论：

- 产品类型：数据密集型金融工作台。
- 推荐风格：`Data-Dense Dashboard`。
- 页面模式：实时运营终端，而不是营销页或动画页。
- 视觉原则：深色工作台、克制色彩、表格/图表/状态优先、可筛选、可追踪。
- 图表原则：K 线使用专业金融图表；异常波动用带标记折线；多 KPI 优先 bullet/紧凑指标，不堆大仪表盘。
- React 规则：异步请求必须处理错误，关键表格/列表状态从服务端派生，复杂交互不要靠本地假状态堆叠。

## 2. 产品定位

新版页面是“私人金融助手控制台”，不是办公室沙盒，不展示隐藏推理链，只展示：

- 已入库事实。
- Agent/Workflow 可审计摘要。
- 工具调用和数据质量。
- 风险反驳。
- 推荐与人工确认状态。
- Finance Memory 和图谱记忆摘要。

真实买卖动作仍需要用户确认，本阶段不自动真实下单。

## 3. 信息架构

左侧固定导航：

1. 总览
2. 持仓监控
3. 观察池
4. 推荐决策
5. 风险中心
6. Agent 运行
7. 中文报告
8. Finance Memory
9. 数据同步
10. 模型配置

第一版前端可以做单页多分区，但导航结构必须按上述能力设计，后续再拆路由。

## 4. 总览首屏

首屏采用金融运营终端布局：

- 顶部状态栏：数据库、图谱、调度器、模型路由、最近刷新时间。
- 左侧主区：今日待处理建议、持仓风险矩阵、推荐排序。
- 右侧上下文区：Agent 决策摘要、风险反驳、证据和记忆引用。
- 底部：触发事件流、Workflow 时间线、数据同步健康。

粒子效果只允许作为非常弱的背景信号流；如果影响阅读，直接移除。

## 5. 后端 API 边界

新增 `src/finance_agent/api/`，使用 FastAPI 作为 Dashboard API。API 只做 HTTP 协议、参数解析、事务边界和序列化，不重新实现金融决策逻辑。

第一批接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | API、数据库、图谱配置和基础服务健康摘要 |
| `GET /api/dashboard/summary?owner_id=` | 总览聚合快照 |
| `GET /api/portfolio/overview?owner_id=` | 用户组合和持仓概览 |
| `GET /api/watchlists?owner_id=` | 活跃观察池条目 |
| `GET /api/recommendations/latest?owner_id=&market=` | 最近可用推荐运行和推荐列表 |
| `GET /api/risks?owner_id=` | 触发事件、风险提醒和数据质量摘要 |
| `GET /api/workflows` | 可调用 Workflow 列表 |
| `POST /api/workflows/run` | 运行金融团队 Workflow |
| `GET /api/workflows/{workflow_run_id}` | 查询 Workflow 审计 |
| `GET /api/reports/{workflow_run_id}` | 读取中文报告 |
| `GET /api/memory/assets/{asset_id}/timeline?owner_id=` | 标的 Finance Memory 时间线 |
| `GET /api/models/config` | 模型供应商、模型实例、路由、检索配置摘要 |
| `PUT /api/models/providers/{provider_key}` | 保存模型供应商连接配置，`api_key` 不传时保留原密钥 |
| `PUT /api/models/instances/{model_key}` | 保存模型实例，并绑定供应商和 Agent 职责 |
| `PUT /api/models/routes/{role}` | 保存 Agent 角色到模型实例的路由规则 |
| `GET /api/models/routes/preview` | 预览当前 Workflow/Agent 会使用的模型，并返回 ready 状态 |
| `GET /api/data/scheduler/status` | 基础数据调度器状态文件健康摘要 |
| `POST /api/chat` | Web 聊天入口，复用 CLI 聊天服务 |

模型配置页不是只读摘要。它必须直接写入数据库配置中心：

- 供应商写入 `model_providers`，用于保存供应商类型、Base URL、超时和密钥。
- 模型实例写入 `model_instances`，用于保存模型 Key、模型名称、供应商 Key 和 Agent 职责。
- Agent 切换写入 `model_routing_rules`，主分析 Agent 使用 `primary_financial_analyst`，高风险复核 Agent 使用 `high_risk_reviewer`。
- 路由预览必须复用 `ModelRoutingPolicy + ModelRuntimeConfigRepository`，确保页面显示的模型就是 Agent/Workflow 实际会路由到的模型。

## 6. 数据状态策略

接口不能伪造业务结果。没有真实数据时返回：

- `empty`：数据库连接正常，但业务表没有对应记录。
- `stale`：状态文件或数据质量提示过期。
- `unavailable`：依赖服务或状态文件不可用。
- `partial`：只有部分数据可以展示。

前端必须显式展示这些状态。

## 7. 前端设计规范

视觉：

- 默认深色。
- 背景接近黑蓝灰，但不能通篇单蓝色。
- 盈利/正向使用绿色，风险使用红色，待确认使用琥珀色。
- 卡片圆角不超过 8px。
- 不做卡片套卡片。
- 文字不能压住图表和按钮。

交互：

- 所有可点击元素必须有 hover 和 focus 状态。
- 长表格必须可滚动或紧凑布局。
- 请求失败、空数据和过期数据必须有状态提示。
- 不展示隐藏推理链，只展示摘要、证据、风险和审计事件。

## 8. 实施范围

本轮实现：

1. 新增 FastAPI 依赖和 API 应用。
2. 新增 Dashboard 聚合服务。
3. 新增 API smoke 脚本。
4. 重构 `apps/agent-office` 为金融控制台。
5. 前端接入 API client，失败时显示本地演示快照和降级状态。
6. 移除旧 Phaser 办公室原型、粒子入口和 `phaser` 依赖，避免前端继续偏向动画演示。
7. 模型配置页支持保存供应商、模型实例和 Agent 路由，并用路由预览验证 Agent 实际切换结果。

暂不实现：

- 真实下单。
- 高级图表库引入。
- 路由拆页。
- 复杂权限系统。
- SSE/WebSocket 实时推送。

## 9. 验收

- `.venv\Scripts\python.exe -m compileall src scripts`
- `.venv\Scripts\ruff.exe check src\finance_agent\api src\finance_agent\application\dashboard_service.py scripts\storage\smoke_dashboard_api.py`
- `.venv\Scripts\python.exe scripts/storage/smoke_dashboard_api.py`
- `npm run build`，工作目录 `apps/agent-office`
- 浏览器打开 `http://127.0.0.1:5173`，桌面和移动宽度均不再展示办公室动画。
- 页面在桌面宽度展示完整控制台，不再是办公室动画。
- API 未连接真实数据库或无业务数据时，前端仍能显示明确降级状态。

说明：全仓 `ruff check src scripts` 当前仍会命中历史文件中的导入排序、长行和少量异常链问题；本轮只要求新增 API、Dashboard 聚合服务和新增 smoke 脚本通过局部 ruff，避免把历史债务混入 Web 控制台重构。
