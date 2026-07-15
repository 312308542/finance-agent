# 方案 10 真实联调与验收实施计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 完成方案 10 T4～T10，以真实模型、真实 PostgreSQL 数据、真实 Web 页面、真实五年回测和当前 Windows 主机证明主链路可用；外部权限、重启授权和长任务边界只记录真实状态，不伪造通过。

**架构：** 联调不改变既有业务规则。T4 使用独立验收 owner 和可清理的数据库样本验证 `model_route → model_review → model_review_result → decision_logs`；T5 使用既有 FastAPI/React 页面完成报告、确认、订单草案、执行登记、持仓、复盘和 Memory 闭环；T6～T8 分别运行数据库回测、Windows 任务计划和基金断点任务。真实联调暴露确定性缺陷时，先走 GitNexus upstream impact，再按 systematic-debugging 与 TDD 修复。

**技术栈：** Python 3.12、pytest、SQLAlchemy/PostgreSQL、FastAPI、React/Vite、PowerShell 5.1 任务计划、GitNexus 1.6.8（WSL CLI fallback）。

---

## 文件结构

- 创建 `docs/superpowers/plans/2026-07-13-scheme10-real-integration-acceptance.md`：保存本轮可复现执行步骤和验收门槛。
- 修改 `docs/开发方案/03-高风险复核回写.md`：追加脱敏真实复核样本和降级证据。
- 修改 `docs/开发方案/06-数据层收尾与部署模板.md`：同步 Windows 注册与基金放量真实状态。
- 修改 `docs/开发方案/07-前端用户闭环页面.md`：追加真实 Web 闭环证据。
- 修改 `docs/开发方案/09-轻量回测与绩效验证.md`：记录真实五年回测及复现结果。
- 修改 `docs/开发方案/10-真实联调与验收.md`：更新 T4～T10 总验收进度和问题清单。
- 修改 `docs/基金行情同步任务方案.md`：更新 FUND-015 水位、失败率和剩余队列。
- 修改 `docs/优化版本进度跟踪表.md`、`docs/项目进度跟踪表.md`、`docs/开发方案/00-总体规划与执行约定.md`：统一项目真实状态。
- 如确定性缺陷需要代码修复，修改文件必须由 GitNexus impact 和红灯测试确定，不在计划中预设无证据的业务改动。

### 任务 1：联调基线与图谱门槛

- [x] **步骤 1：确认 GitNexus 索引新鲜且无并发分析**

运行：

```powershell
wsl.exe -e bash -lc 'cd /mnt/d/Code/aiAgents/finance-agent && gitnexus status'
wsl.exe -e ps -eo pid,args | Select-String 'gitnexus analyze'
```

验收：indexed/current commit 均为 `da40042`，没有运行中的 `gitnexus analyze`。

- [x] **步骤 2：确认代码与前端基线**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix apps\agent-office run build
```

验收：后端 `668 passed`，Vite 生产构建成功。

- [x] **步骤 3：确认真实模型路由与待复核队列**

运行：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli models route-preview --workflow-type recommendation_decision --high-risk
.\.venv\Scripts\python.exe -m finance_agent.cli agent review-pending --owner-id owner:demo --limit 20 --dry-run
```

验收：主分析 `deepseek-v4-pro` 与复核 `gpt-5.5` 均 `ready=true`；dry-run 只读返回待复核事件。

### 任务 2：T4 高风险复核真实回写

**文件：**
- 修改：`docs/开发方案/03-高风险复核回写.md`
- 修改：`docs/开发方案/10-真实联调与验收.md`

- [x] **步骤 1：创建独立、可清理的真实复核样本**

在事务中使用 `AgentWorkflowRunORM`、`AgentWorkflowEventORM`、`DecisionLogORM` 创建：

```text
owner_id = owner:acceptance:scheme10
workflow_run_id = workflow:owner_acceptance_scheme10:recommendation_decision:20260713:t4-real
model_route_event_id = workflow:owner_acceptance_scheme10:recommendation_decision:20260713:t4-real:model_route:1
workflow_event_id = workflow:owner_acceptance_scheme10:recommendation_decision:20260713:t4-real:model_review:1
decision_id = decision:owner_acceptance_scheme10:20260713:t4-real
asset_id = ashare:600519
decision_type = recommendation_sell
review_model = gpt-5.5
review_status = requires_model_review
```

`review_input` 明确记录：没有对应持仓、行情和数据版本不可验证、仍要求卖出并生成订单草案。验收样本只使用命名空间 `owner:acceptance:scheme10`，不得覆盖现有 owner 数据。
同一 run 先写一条 `event_type=model_route` 事件，payload 保存与 `model_review.output.route` 相同的脱敏路由，再写 `model_review`，确保验收的三段审计链是真实事件而不是只存在于嵌套 payload。

- [ ] **步骤 2：运行真实复核模型**

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli agent review-pending --owner-id owner:acceptance:scheme10 --limit 1
```

验收门槛：`processed_count=1`、`rejected_count=1`；若真实模型返回 `needs_human` 或 `approve`，保留原始事实并登记到 T9，不得把样本改写成 reject。

- [ ] **步骤 3：只读验证完整审计链与 decision_logs**

用 SQLAlchemy 只读查询上述 run，按 `created_at` 输出事件类型和 ID；断言：

```text
model_route（协议 payload 中可追溯）
model_review
model_review_result
decision_logs.payload.review_status = rejected_by_review
decision_logs.user_action = rejected_by_review
```

同时确认该 `decision_id` 没有 `order_drafts`；输出只保留模型名、verdict、reasons、token/延迟量级，不输出 key。

- [ ] **步骤 4：用真实错误 key 验证 review_unavailable**

创建临时、独立模型配置：

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli models set-provider --provider-key scheme10-invalid-review --provider-vendor openai --provider-name "方案10错误Key验收" --base-url https://claude.aiapis.help --api-key invalid-for-scheme10-acceptance --timeout-seconds 15
.\.venv\Scripts\python.exe -m finance_agent.cli models set-model --provider-key scheme10-invalid-review --model-key scheme10-invalid-review --model-name gpt-5.5 --role high_risk_reviewer --timeout-seconds 15
```

再创建同 owner 的第二条 `recommendation_sell` 事件，route 指向 `scheme10-invalid-review`，执行 `review-pending --limit 1`。验收：`unavailable_count=1`、源事件仍为 `review_unavailable` 可重试、`decision_logs.payload.review_confidence_multiplier=0.7`、没有订单草案。验证后删除临时 provider/model 与验收样本，真实 `gpt-5.5` 配置不得改动。

- [x] **步骤 5：若暴露缺陷则先图谱 impact，再 TDD 修复**

对实际待改函数/类执行 `gitnexus impact -r finance-agent --direction upstream`，目标参数必须是图谱返回的精确符号名或 UID，并把 direct callers、affected processes 和 risk level 记录到联调日志。

HIGH/CRITICAL 先向用户告警。读取 `superpowers-zh:systematic-debugging` 与 `test-driven-development`，先补能复现真实失败的测试并确认红灯，再做最小实现；专项测试和全量测试都必须通过。

- [x] **步骤 6：记录脱敏样本并提交 T4 文档**

在方案 03/10 记录事件 ID、verdict、状态转换、错误 key 降级、无订单草案证明和命令输出摘要。暂存后运行：

```bash
gitnexus detect-changes -r finance-agent --scope staged --max-files 50 --max-hunks 300 --timeout-ms 25000
```

当前外部额度阻塞时提交：`docs(联调): 记录高风险复核额度阻塞`；只有 reject 门槛满足后才使用“完成高风险复核真实回写验收”。

> 2026-07-13 执行记录：步骤 2 已真实执行，但供应商返回 HTTP 403 `insufficient_user_quota`，因此 reject 验收门槛未满足，步骤 2～4 保持未勾选。review_unavailable 审计链、0.7 置信度惩罚和零订单草案已真实通过；连接测试 HTML 200 假阳性已按 TDD 修复并提交 `72e1ac6`。补充额度后复用同一事件重跑。

### 任务 3：T5 端到端主链路与前端闭环

**文件：**
- 修改：`docs/开发方案/07-前端用户闭环页面.md`
- 修改：`docs/开发方案/10-真实联调与验收.md`

- [x] **步骤 1：启动本地 API 与前端**

API 只绑定 `127.0.0.1:8000`，前端使用项目现有 Vite 配置；先以 HTTP 健康检查证明服务可用，不修改 Windows 任务计划。

- [x] **步骤 2：创建真实 Workflow 报告**

使用当前有效推荐 run 和 `owner:acceptance:scheme10` 运行 `recommendation_decision`，模型角色先限定为 `risk_rebuttal`，记录 workflow/report/decision ID；若数据闸门拒绝过期数据，先运行既有数据刷新任务，不绕过闸门。

- [x] **步骤 3：使用 in-app browser 完成用户动作**

调用 `browser:control-in-app-browser`，依次验证：报告页可见 → 推荐确认接受 → 生成订单草案 → 页面只有“去外部登记/执行登记”而无自动下单措辞 → 登记外部执行 → 持仓更新 → 复盘任务生成。

- [x] **步骤 4：数据库核验闭环**

按上一步 ID 只读核验 `decision_logs.user_action`、`order_drafts`、`execution_records`、`positions`、复盘审计事件和 `assistant_memories`。每个下游记录必须引用前一节点 ID；不得用页面截图替代数据库引用证明。

- [x] **步骤 5：前端专项与生产构建**

```powershell
node apps\agent-office\scripts\test-action-loop-view.mjs
node apps\agent-office\scripts\test-recommendation-view.mjs
node apps\agent-office\scripts\test-report-view.mjs
node apps\agent-office\scripts\test-chat-stream.mjs
npm --prefix apps\agent-office run build
```

如页面行为缺陷需要修复，先使用 `frontend-ui-verification`、GitNexus impact 和 TDD；修复后保留浏览器截图与 DOM/网络证据。

- [x] **步骤 6：记录证据并提交 T5 文档**

方案 07/10 记录脱敏 ID、数据库引用链、浏览器证据和红线检查。staged detect-changes 完整后提交：`docs(联调): 完成前端用户闭环真实验收`。

### 任务 4：T6 真实五年回测

**文件：**
- 修改：`docs/开发方案/09-轻量回测与绩效验证.md`
- 修改：`docs/开发方案/10-真实联调与验收.md`

- [x] **步骤 1：运行真实五年 TopN 回测**

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli backtest run --strategy factor_score_topn --universe universe:merged:ashare:recommendation --strategy-id strategy:ashare:short_swing --years 5 --score-mode replayed --topn 20 --rebalance monthly
```

验收：状态不是因缺数据产生的 `partial`；CAGR、最大回撤、夏普、Sortino 均为有限数，`data_versions` 含 bars 水位、`score_mode=replayed`、策略/候选池/时间窗口。

- [x] **步骤 2：同参数重跑并比较**

再次运行同命令；比较核心指标、入选资产和数据版本。相同数据库水位下结果必须一致；水位变化则记录版本差异，不声称完全可复现。

- [x] **步骤 3：人工合理性审阅并记录**

检查年化、回撤、夏普和换手量级，确认没有 NaN、无穷大、单日未来数据或夸张收益；文档明确这是“历史模拟回放”，不是实盘收益。

- [x] **步骤 4：提交 T6 文档**

方案 09/10 写入两个 backtest ID、参数、指标、数据版本和审阅结论。staged detect-changes 后提交：`docs(回测): 完成真实五年绩效验收`。

### 任务 5：T7 Windows 任务计划真实注册

**文件：**
- 修改：`docs/开发方案/06-数据层收尾与部署模板.md`
- 修改：`docs/开发方案/10-真实联调与验收.md`

- [x] **步骤 1：导出可再生调度配置并预演**

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli data config export --output runtime\base_data_scheduler\base_data_scheduler.json
.\deploy\windows\register_scheduler_task.ps1 -ProjectRoot $PWD -WhatIf
.\deploy\windows\register_api_task.ps1 -ProjectRoot $PWD -WhatIf
```

- [ ] **步骤 2：真实注册并查询任务定义**

```powershell
.\deploy\windows\register_scheduler_task.ps1 -ProjectRoot $PWD
.\deploy\windows\register_api_task.ps1 -ProjectRoot $PWD
Get-ScheduledTask -TaskName FinanceAgent-BaseDataScheduler,FinanceAgent-Api | Get-ScheduledTaskInfo
```

验收：两任务触发器为 AtStartup、RestartCount=3、API 只绑定 127.0.0.1。若 `Access is denied`，记录当前令牌和命令原文为权限阻塞，继续后续任务。

- [ ] **步骤 3：无重启先做启动/失败恢复验证**

手动 `Start-ScheduledTask`，HTTP 检查 API，读取 scheduler status 心跳；停止任务进程后确认任务设置允许重启。不得为了通过验收修改系统安全策略。

- [x] **步骤 4：记录重启授权边界**

整机重启会中断用户会话，只有获得明确授权后才执行。未授权时将“重启后自动恢复”保留为人工阻塞，文档不得标记全通过。

> 2026-07-13 执行记录：调度配置已导出到 `runtime\base_data_scheduler\base_data_scheduler.json`（55,553 bytes），两个注册脚本的 `-WhatIf` 均退出 0。随后使用当前命令原文真实注册 `FinanceAgent-BaseDataScheduler` 和 `FinanceAgent-Api`，两次均返回 `HRESULT 0x80070005 / Access is denied`；当前用户为 `DESKTOP-ELT87C4\Administrator`，但令牌 `IsAdmin=False`、完整性级别为 Medium，查询确认两项任务均不存在。真实失败还暴露脚本会继续打印“Registered”并退出 0 的误导行为；GitNexus upstream impact 为 LOW、0 个直接调用者、0 条流程，按 TDD 增加失败终止测试后，为两个 `Register-ScheduledTask` 显式添加 `-ErrorAction Stop`。同一权限场景复跑时两个子进程均退出 1、包含 `Access is denied`、不再包含成功提示，任务数仍为 0。步骤 2 验收门槛未满足，步骤 3 无任务可启动，保持未勾选；当前手工启动的 `127.0.0.1:8000/api/health` 仍返回 `status=ok`，但不能替代计划任务验证。整机重启未经授权，未执行。

### 任务 6：T8 基金断点放量

**文件：**
- 修改：`docs/基金行情同步任务方案.md`
- 修改：`docs/开发方案/06-数据层收尾与部署模板.md`
- 修改：`docs/开发方案/10-真实联调与验收.md`

- [x] **步骤 1：导出最新计划并确认三项手动任务**

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli data config export --output runtime\base_data_scheduler\scheme10-fund-bootstrap.json
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\scheme10-fund-bootstrap.json --print-plan
```

> 2026-07-13 执行记录：配置导出和 `--print-plan` 均退出 0，三项 bootstrap 都存在，参数为 `batch_size=50`、`max_workers=2`、`lookback=10y`、`source_limit=null`。首次对 ETF 运行同参数 `--dry-run` 得到 `enabled_jobs=0 / jobs=[]`，定位为 `--only` 仅筛选任务但没有临时启用配置中 `enabled=false` 的 manual job；若不修复，计划中的三条正式命令会假成功但不采集。GitNexus 对 CLI `main` 的 upstream impact 为 LOW，仅脚本文件入口 1 个直接上游、0 条流程；TDD 将现有 CLI 测试改为显式选择禁用 manual job，先得到 1 failed，再只在 `--run-once --only` 场景临时 `replace(job, enabled=True)`。测试转绿后真实 ETF dry-run 为 `enabled_jobs=1 / job_count=1 / status=planned`，常驻配置中的 manual job 仍保持禁用。首次真实 ETF 运行又发现每只 `item_count=5`：`lookback=10y` 虽换算出日期，但 job 顶层 `limit=null` 没有覆盖采集脚本样例默认值 5。为避免扩大伪放量，处理 55 只后立即停止完整进程树并保留日志。GitNexus 对共享 `BaseDataScheduler.build_collection_args` 的 upstream impact 为 HIGH（7 个直接调用者、29 个总上游、4 个模块），因此只对基金的 `market_bars_full_history_backfill` / `fund_nav_full_history_backfill` 显式传 `limit=None`；TDD 红灯确认默认 5 未清除，最小修复后专项 3 passed，真实 ETF dry-run 已显示 `limit=null`、十年日期窗口。正确重跑耗时 45 分 47 秒，共 1,507 个 ETF 任务，1,505 available、2 locked（159125/159128，来自首轮强制停止遗留锁），真实失败 0。验收查询随后暴露 64MB `/dev/shm` 下 1,507 ID 覆盖聚合的 PostgreSQL 动态共享内存耗尽；复用 A 股分块模式后，基金日 K/净值及年度覆盖均按 200 ID 分块，真实 826 万行库查询成功。另补齐成功水位的 `requested_start/requested_end/sync_task_type`，并让完整请求水位只信任成立前和源端最新日后的边界空窗、不放过中间整年缺口；基于本次成功时间窗受限补写并验证 1,505 行。真实 resolver 最终只剩两只 locked 标的，待提交后补跑。

> 2026-07-14 至 2026-07-15 续执行记录：ETF 1,507 只和 LOF 390 只已收口，`fund_market_bars` 水位共 1,897 条且全部 `available`，LOF 日 K 共 708,159 行。开放式基金一次性处理 20,604 只时在前两只后触发 `py_mini_racer`/V8 崩溃，确认不是 venv 解释器泄漏，而是无界长任务稳定性和 6 小时总超时风险；先 TDD 将全历史结束日固定为前一个完整自然日，再将 bootstrap 固定为单 worker、`source_limit=500`，提交 `80a6746`、`1f1b153`，全量回归均为 682 passed。第一、第二批各 500 只均为 500/500 `available`、0 `error`。第三批主任务 499/500，唯一失败 `002612` 为东财瞬时连接超时；同一 URL 随后 0.428 秒返回 HTTP 200，单标的重试写入 2,387 行并将失败水位清零，请求边界校正为 `20160715..20260714`。当前数据库覆盖 1,509 只、3,905,196 行，全部 `available`，最新净值日 2026-07-14；剩余 19,095 只。步骤 2 仍不勾选，直到 20,604 只全部完成或失败项形成可重跑清单并满足失败率门槛。

> 2026-07-15 后续记录：第四、第五批的 4 个瞬时超时项均已单标的重试成功。第六批 `005265` 重试成功，`005471` 确认为源端结构性无历史净值；真实故障定位到 unavailable 被写为无冷却 error 并重复入队，已按 TDD 新增终态水位、同窗口排除和窗口扩大/手工重验语义，提交 `5223f6a`，全量 689 passed。第七批 499 `available`、1 `unavailable`；`005792` 同样为 HTTP 200 且空历史趋势，终态水位生效。当前事实表覆盖 3,507 只、7,909,335 行，另有 2 条终态失败水位；同窗口 resolver 剩余 17,122 个候选，第八批已启动。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第八批 500/500 `available`；第九批 `006922` 为结构性 `unavailable`；第十批 `007665`、第十二批 `009133` 均为瞬时连接超时，单标的重试成功；第十一批 500/500 `available`。前十二批累计 6,000 个任务，最终 5,997 `available`、3 个结构性 `unavailable`；事实表覆盖 6,006 只、11,662,208 行，事实行非 available 数为 0，同窗口 resolver 剩余 14,629 个候选，第十三批已启动。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第十四批 500/500 `available`。累计前十四批 7,000 个任务，最终 6,997 `available`、3 个结构性 `unavailable`；事实表覆盖 7,006 只、12,926,944 行，事实行非 available 数为 0，同窗口 resolver 剩余 13,629 个候选，第十五批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第十五批初次结果为 498 `available`、2 个瞬时 `error`（`010802`、`010966`），协调器按失败即停；随后逐标的真实重试均成功，分别写入 1,290、1,298 条净值，未发现结构性空历史。累计前十五批 7,500 个任务最终 7,497 `available`、3 个结构性 `unavailable`；事实表覆盖 7,506 只、13,552,422 行，事实行非 available 数为 0，同窗口 resolver 剩余 13,129 个候选，第十六批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第十六批 500/500 `available`、0 `error`、0 `unavailable`。累计前十六批 8,000 个任务最终 7,997 `available`、3 个结构性 `unavailable`；事实表覆盖 8,006 只、14,148,174 行，事实行非 available 数为 0，同窗口 resolver 剩余 12,629 个候选，第十七批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第十七批初次结果为 499 `available`、`011978` 瞬时超时，协调器按失败即停；随后逐标的真实重试成功并写入 1,145 条净值。累计前十七批 8,500 个任务最终 8,497 `available`、3 个结构性 `unavailable`；事实表覆盖 8,506 只、14,718,416 行，事实行非 available 数为 0，同窗口 resolver 剩余 12,129 个候选，第十八批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第十八批初次结果为 499 `available`、`012703` 瞬时超时，协调器按失败即停；随后逐标的真实重试成功并写入 1,076 条净值。累计前十八批 9,000 个任务最终 8,997 `available`、3 个结构性 `unavailable`；事实表覆盖 9,006 只、15,273,802 行，事实行非 available 数为 0，同窗口 resolver 剩余 11,629 个候选，第十九批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第十九批 500/500 `available`、0 `error`、0 `unavailable`。累计前十九批 9,500 个任务最终 9,497 `available`、3 个结构性 `unavailable`；事实表覆盖 9,506 只、15,808,520 行，事实行非 available 数为 0，同窗口 resolver 剩余 11,129 个候选，第二十批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十批 500/500 `available`、0 `error`、0 `unavailable`。累计前二十批 10,000 个任务最终 9,997 `available`、3 个结构性 `unavailable`；事实表覆盖 10,006 只、16,319,074 行，事实行非 available 数为 0，同窗口 resolver 剩余 10,629 个候选，第二十一批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十一批 500/500 `available`、0 `error`、0 `unavailable`。累计前二十一批 10,500 个任务最终 10,497 `available`、3 个结构性 `unavailable`；事实表覆盖 10,506 只、16,816,900 行，事实行非 available 数为 0，同窗口 resolver 剩余 10,129 个候选，第二十二批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十二批 500/500 `available`、0 `error`、0 `unavailable`。累计前二十二批 11,000 个任务最终 10,997 `available`、3 个结构性 `unavailable`；事实表覆盖 11,006 只、17,288,529 行，事实行非 available 数为 0，同窗口 resolver 剩余 9,629 个候选，第二十三批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十三批 500/500 `available`、0 `error`、0 `unavailable`。累计前二十三批 11,500 个任务最终 11,497 `available`、3 个结构性 `unavailable`；事实表覆盖 11,506 只、17,733,402 行，事实行非 available 数为 0，同窗口 resolver 剩余 9,129 个候选，第二十四批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十四批 500/500 `available`、0 `error`、0 `unavailable`。累计前二十四批 12,000 个任务最终 11,997 `available`、3 个结构性 `unavailable`；事实表覆盖 12,006 只、18,138,912 行，事实行非 available 数为 0，同窗口 resolver 剩余 8,629 个候选，第二十五批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十五批初次结果为 498 `available`、`017393/017488` 瞬时超时，协调器按失败即停；随后逐标的真实重试成功，分别写入 884、856 条净值。累计前二十五批 12,500 个任务最终 12,497 `available`、3 个结构性 `unavailable`；事实表覆盖 12,506 只、18,525,013 行，事实行非 available 数为 0，同窗口 resolver 剩余 8,129 个候选，第二十六批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十六批 500/500 `available`、0 `error`、0 `unavailable`。累计前二十六批 13,000 个任务最终 12,997 `available`、3 个结构性 `unavailable`；事实表覆盖 13,006 只、18,880,311 行，事实行非 available 数为 0，同窗口 resolver 剩余 7,629 个候选，第二十七批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十七批初次结果为 498 `available`、`018938/018941` 瞬时超时，协调器按失败即停；随后逐标的真实重试成功，分别写入 542、703 条净值。累计前二十七批 13,500 个任务最终 13,497 `available`、3 个结构性 `unavailable`；事实表覆盖 13,506 只、19,199,693 行，事实行非 available 数为 0，同窗口 resolver 剩余 7,129 个候选，第二十八批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十八批初次结果为 496 `available`、`019221/019578` 瞬时超时、`019738/019739` `unavailable`；两个瞬时项重试成功并写入 526、670 条，两个无历史项复验仍为 `unavailable`，确认为结构性缺口。累计前二十八批 14,000 个任务最终 13,997 `available`、5 个结构性 `unavailable`；事实表覆盖 14,004 只、19,494,531 行，事实行非 available 数为 0，同窗口 resolver 剩余 6,629 个候选，第二十九批已启动并正在运行。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第二十九批结果为 497 `available`、`005792/006922/020490` `unavailable`；`020490` 以及此前状态漂移的 `019738/019739` 均使用 `--no-schedule-failure-retry` 逐标的复验，确认东财响应为空历史并将水位统一落为 `unavailable`。累计前二十九批 14,500 个任务最终 14,497 `available`、6 个结构性 `unavailable`；事实表覆盖 14,501 只、19,838,793 行，事实行非 available 数为 0，同窗口 resolver 剩余 6,098 个候选，第二十九批已收口。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第三十批结果为 497 `available`、`019738/019739/020490` 已知结构性 `unavailable`，无新增缺口；累计前三十批 15,000 个任务最终 14,994 `available`、6 个结构性 `unavailable`。事实表覆盖 14,998 只、20,090,614 行，事实行非 available 数为 0，同窗口 resolver 剩余 5,601 个候选，第三十批已收口。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第三十一批结果为 497 `available`、`021487/021743/021834` `unavailable`；三只均使用 `--no-schedule-failure-retry` 逐标的复验，仍为空历史并写终态水位。累计前三十一批 15,500 个任务最终 15,491 `available`、9 个结构性 `unavailable`；事实表覆盖 15,495 只、20,315,629 行，事实行非 available 数为 0，同窗口 resolver 剩余 5,101 个候选，第三十一批已收口。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第三十二批为 496 `available`、`022131` 瞬时超时、`021487/021743/021834` 三个既有结构性 `unavailable`；`022131` 单标的重试成功。第三十三批 500/500 `available`。当前事实表覆盖 16,492 只、20,671,966 行，唯一水位为 16,491 `available`、9 `unavailable`，从未尝试基金 4,103 只。动态窗口推进暴露成立前空窗信任丢失，导致 11,719 只已覆盖新结束日的基金误入队；TDD 修复后当前窗口 resolver 从 17,634 降至 6,393，首批仍为未初始化基金，9 个终态项全部排除。步骤 2/3 继续保持未勾选。

> 2026-07-15 批次续记：第三十四批初次为 498 `available`、`023663/023875` 两个瞬时连接超时；逐标的真实重试均成功，写入 257/308 条净值。当前事实表覆盖 16,992 只、20,804,614 行，唯一水位为 16,991 `available`、9 `unavailable`，从未尝试基金 3,603 只。步骤 2/3 继续保持未勾选。

- [ ] **步骤 2：按 ETF、LOF、开放式基金顺序分批执行**

```powershell
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\scheme10-fund-bootstrap.json --run-once --only fund.etf.bars.1d.bootstrap
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\scheme10-fund-bootstrap.json --run-once --only fund.lof.bars.1d.bootstrap
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\scheme10-fund-bootstrap.json --run-once --only fund.open.nav.bootstrap
```

每个任务完成后再启动下一项，禁止并行冲击上游接口；按水位统计 available/error/unavailable、失败率和剩余队列。

- [ ] **步骤 3：复跑失败水位并验收**

仅重跑冷却已结束的失败项。验收：三类失败率均小于 5%，失败项有明确 provider/error/watermark 可继续重跑；若外部接口持续不可用，记录真实阻塞和已覆盖数量，不把“部分放量”写成“全量完成”。

### 任务 7：T9 问题清单、T10 文档同步和方案 10 收尾

- [ ] **步骤 1：登记真实问题清单**

在方案 10 分成三类：确定性代码缺陷（已 TDD 修复并附提交）、模型质量问题（不改裁决规则）、外部权限/网络/重启阻塞（附复现命令和下一动作）。

- [ ] **步骤 2：逐项同步权威文档**

同步方案 03/06/07/09/10、基金任务方案、优化进度表、项目进度表和 00 总览。未完成项保持未勾选；历史状态与当前证据矛盾时以本轮命令、数据库和页面证据为准。

- [ ] **步骤 3：运行文档、后端和前端总回归**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix apps\agent-office run build
git diff --check
```

当前仓库没有独立的进度文档 pytest；文档一致性通过逐项回读任务表、`git diff --check`、全量 pytest、前端构建和 staged GitNexus 共同验证，不引用已不存在的测试文件。

- [ ] **步骤 4：staged GitNexus 检测并提交**

确认 `.ai/`、`.codex/`、`artifacts/`、`runtime/`、密钥未暂存；运行完整 staged detect-changes，提交：`docs(方案): 完成统一真实联调阶段验收`。

- [ ] **步骤 5：进入下一依赖项目**

重读方案 15、18、22 和待决策记录，按“方案 15 数据覆盖 → 方案 18 权重固化 → 方案 22 黄金样本/CHoCH → D-014 与文档卫生”继续目标，不因方案 10 的外部阻塞停止可独立推进的工作。
