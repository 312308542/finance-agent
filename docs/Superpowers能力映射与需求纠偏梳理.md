# Superpowers 能力映射与需求纠偏梳理

更新时间：2026-05-17

本文用于重新校准 `finance-agent` 的产品目标、当前实现偏差，以及 `superpowers-zh` 中哪些能力适合被本项目使用。

结论先行：`superpowers-zh` 不是金融业务框架，也不是可直接复用的选股、风控或交易 Agent 团队。它更像一套 AI 编程和 Agent 协作的方法论工具箱，适合约束我们的研发流程、规格编写、任务拆解、验证、MCP 工具建设和多角色工作流原型。真正的金融业务运行时，仍应由本项目自己的 `PersonalFinanceAgent`、`FinancialTeamWorkflow`、Portfolio、Watchlist、Finance Memory、数据采集、因子、评分、风险和报告服务承担。

## 1. 当前偏差判断

当前代码实现已经打通了一条确定性推荐数据链路：

```text
候选池
 -> 指标计算
 -> 因子计算
 -> 初筛
 -> 多维评分
 -> 信号快照
 -> 推荐排序
 -> 推荐结果落库
```

这条链路是必要的，但它还不是用户真正需要的完整产品。

用户最终需求更接近：

```text
持仓 + 私人观察池 + 用户偏好 + Finance Memory
 -> 实时/定时行情与风险监控
 -> 触发条件判断
 -> PersonalFinanceAgent 调度金融团队 Workflow
 -> 专业分析、风险反驳、换股比较、推荐排序
 -> 买入/卖出/加仓/减仓/换股/继续观察建议
 -> 中文解释报告
 -> 用户确认/反馈
 -> 决策日志、观察池状态流转、Finance Memory、复盘任务写入
```

因此偏差不是“数据链路做错了”，而是研发重心还停留在底层推荐流水线，没有尽快闭合上层私人金融助手的长期运行闭环。

## 2. Superpowers-zh 的本质定位

`superpowers-zh` 适合回答的是：

- AI 助手在动手前如何澄清需求。
- 如何把设计拆成可执行计划。
- 如何按计划实现并验证。
- 如何使用多 Agent 或多角色协作完成研发任务。
- 如何构建 MCP 工具。
- 如何做 TDD、系统化调试、代码审查和完成前验证。
- 如何保持中文文档、中文提交和国内 Git 工作流习惯。

它不适合直接回答：

- 股票或数字货币应该买什么。
- 如何计算因子、信号、评分。
- 如何作为长期持仓监控服务。
- 如何替代金融记忆系统。
- 如何替代 LangGraph / WorkflowService 成为生产运行时。
- 如何直接充当金融分析师 Agent 角色库。

一句话：`superpowers-zh` 应该进入“研发方法层”和“Agent 编排设计参考层”，不应该直接进入“金融业务运行时核心”。

## 3. 可复用能力矩阵

| Superpowers 能力 | 是否适合本项目 | 建议使用位置 | 用法边界 |
| --- | --- | --- | --- |
| `brainstorming` | 适合 | 需求变更、架构纠偏、Agent 角色设计前 | 用于防止直接写代码导致方向偏移；不进入运行时 |
| `writing-plans` | 强烈适合 | 每个阶段的实现计划 | 把私人金融助手拆成可执行任务，明确文件、测试、验证命令 |
| `executing-plans` | 适合 | 当前会话按计划推进时 | 适合单线程逐步实现；要保留验证检查点 |
| `subagent-driven-development` | 适合 | 任务可拆成互不冲突模块时 | 用于并行开发 Portfolio、Watchlist、Memory、Workflow 等独立模块 |
| `dispatching-parallel-agents` | 适合 | 代码探索、文档核对、独立验证任务 | 只适合研发期并行，不等于产品内多 Agent 团队 |
| `workflow-runner` | 部分适合 | 金融团队 Workflow 原型和评审 | 可借鉴 YAML 多角色 DAG 思路；生产运行时仍建议用本项目 `WorkflowService` / LangGraph |
| `mcp-builder` | 适合 | 后续暴露 Finance MCP 工具 | 用于设计 Hermes 调用的 MCP tools；不承载业务逻辑 |
| `test-driven-development` | 适合 | 服务层、仓储层、工作流节点实现 | 特别适合 Finance Memory、观察池状态机、触发策略这类易回归模块 |
| `systematic-debugging` | 适合 | AKShare、Binance、TimescaleDB、调度链路排障 | 先定位事实，再做修复；符合当前项目需要真实验证的习惯 |
| `verification-before-completion` | 强烈适合 | 每次声称完成前 | 必须用测试、脚本、数据库记录或运行输出来证明闭环 |
| `requesting-code-review` | 适合 | 关键模块完成后 | 用于找风险、缺测试、架构越界 |
| `receiving-code-review` | 适合 | 处理审查反馈 | 防止盲目接受不适合本项目目标的修改 |
| `using-git-worktrees` | 可选 | 大规模并行开发时 | 当前项目已有不少文档改动，使用前要避免覆盖用户变更 |
| `finishing-a-development-branch` | 可选 | 分支收尾、提交、PR 前 | 本项目提交信息默认中文 |
| `writing-skills` | 适合 | 沉淀项目级专属 skill | 可后续创建 `finance-personal-assistant-development`，把本项目约束固定下来 |
| `chinese-documentation` | 适合 | 中文文档规范参考 | 它是手动参考型能力，不需要进入运行时 |
| `chinese-commit-conventions` | 适合 | 中文 commit 规范参考 | 本项目后续 commit message 默认中文 |
| `chinese-code-review` | 可选 | 中文审查沟通模板 | 只在需要统一 review 口径时使用 |
| `chinese-git-workflow` | 可选 | 国内 Git 平台接入 | 当前不是主要矛盾 |

## 4. 不建议复用的方式

以下做法会继续扩大偏差：

- 把 `superpowers-zh` 的 skill 名称直接映射成金融分析 Agent。
- 让 `workflow-runner` 直接承担生产级持仓监控和推荐工作流。
- 把 `brainstorming`、`writing-plans` 等研发技能放进用户可见的金融助手能力菜单。
- 把中文文档、中文提交、代码审查技能误认为金融业务能力。
- 用多 Agent 自由讨论替代确定性数据、因子、评分、风险和证据链。

金融建议必须基于结构化事实、可追踪证据、用户持仓、观察池、风险约束和历史决策，而不是基于“Agent 讨论得像不像专家”。

## 5. 对 Agent 层的修正建议

推荐采用两层 Agent 架构。

### 5.1 上层：PersonalFinanceAgent

上层主 Agent 应该更像 Hermes-agent / Vibe-Trading 风格的高自由度私人助手，但必须被本项目工具边界约束。

职责：

- 读取持仓、观察池、用户偏好和 Finance Memory。
- 接收定时、实时波动、用户询问、风险触发等事件。
- 判断应该调用哪个底层 Workflow。
- 管理提醒、复盘任务、决策日志和观察池状态流转。
- 给用户输出可理解、可确认、可追溯的建议。

不允许：

- 直接抓取 AKShare、ccxt、Binance 数据。
- 直接计算技术指标、因子和评分。
- 绕过风险反驳给出强买入结论。
- 绕过用户确认执行真实下单。

### 5.2 底层：FinancialTeamWorkflow

底层金融团队更适合做成固定、可审计、可复盘的工作流，而不是无限自由 loop。

建议节点：

- `fundamental_or_project_analyst`：A 股基本面或数字货币项目面。
- `technical_analyst`：趋势、波动、支撑压力、信号冲突。
- `capital_or_derivatives_analyst`：A 股资金流或数字货币资金费率、未平仓量、多空比。
- `event_analyst`：新闻、公告、财报、链上或宏观事件。
- `risk_rebuttal`：反方观点、失效条件、数据缺失和尾部风险。
- `portfolio_decision`：结合持仓、仓位、候选标的、评分和风险，输出操作建议。
- `report_writer`：整理中文解释报告，不重新发明事实。

这里可以借鉴 `workflow-runner` 的多角色 DAG 思路，但生产实现应落在：

```text
src/finance_agent/agents/workflows/
src/finance_agent/application/workflow_service.py
agent_workflow_runs
agent_workflow_events
```

## 6. 需求闭环应优先补齐的能力

当前最应该从“继续扩展数据源”切换到“补私人金融助手闭环”。

### 6.1 第一闭环：持仓监控

目标：

```text
Position
 -> 最新行情/信号/风险
 -> 触发规则
 -> FinancialTeamWorkflow.portfolio_monitoring
 -> 持有/加仓/减仓/卖出候选/风险提醒
 -> DecisionLog
 -> Finance Memory / ReviewTask
```

优先实现：

- `PortfolioService`
- `Position` 读取和标准化
- `TriggerPolicy`
- `AlertService`
- `portfolio_monitoring` Workflow 输入/输出协议
- `DecisionLogService`

### 6.2 第二闭环：私人观察池管理

目标：

```text
推荐结果 / 用户手动关注
 -> WatchlistItem + AssetThesis
 -> 启动条件 / 失效条件
 -> 定时跟踪
 -> 升级为买入候选、继续观察、暂停或移除
 -> 观察理由写入 Finance Memory
```

优先实现：

- `WatchlistService`
- `AssetThesis`
- `watchlist_management` Workflow
- 观察项状态机
- 观察池与推荐结果同步规则

### 6.3 第三闭环：推荐到行动建议

现有 `UniverseRecommendationPipeline` 只到推荐排序，还需要接：

```text
AssetRecommendation
 -> AgentContextBuilder
 -> FinancialTeamWorkflow.asset_recommendation
 -> RiskRebuttal
 -> PortfolioDecision
 -> 中文报告
 -> 用户反馈
 -> DecisionLog / Memory
```

这一步才能回答用户真正关心的“现在我该不该买、卖、换、继续观察”。

## 7. Superpowers 在本项目中的推荐落地方式

### 7.1 不把它作为生产依赖

`superpowers-zh` 不应该进入 `pyproject.toml` 作为金融助手运行时依赖，也不应该被业务代码 import。它应该作为研发工作流和项目协作规范存在。

### 7.2 增加项目级使用规范

可以在后续新增或补充：

```text
docs/superpowers/
  specs/
  plans/
```

用途：

- 重大需求先写 spec。
- 进入实现前写 plan。
- 每个 plan 明确文件、测试和验证命令。
- 关键闭环完成前使用完成前验证。

### 7.3 后续创建项目专属 skill

等本轮架构稳定后，可以用 `writing-skills` 创建一个项目级 skill：

```text
finance-personal-assistant-development
```

它只负责提醒 AI 助手遵守本项目边界：

- 目标是私人金融助手，不是泛金融平台。
- A 股和数字货币都要覆盖。
- 推荐和真实下单分离。
- 数据、因子、评分、风险由确定性服务负责。
- Agent 只做调度、分析、反驳、解释、记忆和复盘。
- Finance Memory 与 Hermes Memory 分层。
- PostgreSQL + TimescaleDB 是全环境统一事实源。
- 提交、注释、文档默认中文。

这个项目级 skill 可以降低后续会话再次偏航的概率。

## 8. 建议的下一步执行顺序

### 第一步：冻结继续横向扩数据源

AKShare 能力还可以继续补，但现在不应成为主线。后续新增数据源必须回答一个问题：它支撑哪个闭环的哪个判断？

例如：

- 支撑持仓风险触发。
- 支撑观察池启动条件。
- 支撑换股比较。
- 支撑风险反驳。
- 支撑中文报告证据引用。

### 第二步：补 M2 数据库迁移

优先落地：

- `portfolios`
- `positions`
- `watchlists`
- `watchlist_items`
- `asset_theses`
- `monitoring_alerts`
- `decision_logs`
- `assistant_memories`
- `memory_embeddings`
- `financial_memory_edges`
- `review_tasks`
- `agent_workflow_runs`
- `agent_workflow_events`

这些表是私人金融助手闭环的地基。

### 第三步：补应用服务层

优先落地：

- `application/portfolio_service.py`
- `application/watchlist_service.py`
- `application/memory_service.py`
- `application/decision_log_service.py`
- `application/alert_service.py`
- `application/workflow_service.py`

### 第四步：补工作流协议，不急着接复杂 LLM

先定义结构化输入/输出：

- `PortfolioMonitoringInput`
- `WatchlistManagementInput`
- `AssetRecommendationWorkflowInput`
- `SwapDecisionInput`
- `DailyReviewInput`
- `WorkflowDecision`
- `RiskRebuttal`
- `ChineseReport`

第一版可以先用规则或 stub 输出跑通审计和落库，再接 LLM。

### 第五步：接 Hermes / MCP

等服务层稳定后，再用 `mcp-builder` 的方法论设计 MCP 工具：

- `get_portfolio_status`
- `monitor_positions`
- `list_watchlist`
- `add_watchlist_item`
- `run_asset_recommendation`
- `compare_swap_candidate`
- `write_user_feedback`
- `get_decision_history`

MCP 工具只调用 application service，不写重复业务逻辑。

## 9. 后续验收标准

判断项目是否回到正确方向，不看 Agent 数量，也不看数据源数量，而看是否能完成这些真实场景：

1. 用户导入或配置一组持仓，系统能给出当前风险、盈利/亏损、仓位和下一步建议。
2. 某只观察股或币触发启动条件，系统能说明为什么值得继续关注或升级为买入候选。
3. 当前持仓变弱、候选标的变强时，系统能做换股/换币比较，而不是只给两个孤立评分。
4. 每次建议都能追溯到数据、信号、风险、证据、历史记忆和用户反馈。
5. 用户拒绝或采纳建议后，系统能写入决策日志，并影响下一次建议。
6. 每日复盘能产出“今天发生了什么、哪些建议有效、哪些观察条件变化、明天看什么”。

如果这些场景没跑通，即使我们接入再多 AKShare 接口、再多 Agent 角色，也仍然没有完成私人金融助手。

## 10. 总结

`superpowers-zh` 对本项目最大的价值不是“提供金融 Agent”，而是帮助我们把研发过程拉回正确节奏：

```text
先澄清目标
 -> 写规格
 -> 写计划
 -> 小步实现
 -> 每步验证
 -> 审查风险
 -> 沉淀文档和记忆
```

`finance-agent` 的业务主线则应该明确收敛为：

```text
私人持仓
 -> 私人观察池
 -> 数据与因子
 -> 评分与信号
 -> 金融团队 Workflow
 -> 风险反驳
 -> 操作建议
 -> 中文解释
 -> 用户反馈
 -> Finance Memory
```

下一阶段应优先补齐 Portfolio、Watchlist、Alert、DecisionLog、Finance Memory 和 WorkflowService，而不是继续把系统扩成一个泛金融数据工具箱。
