# Penpot 原型说明

本目录用于记录 Hermes 私人金融助手在 Penpot 中生成的可编辑原型产物。最新定位不是单次选股页面，而是“上层 `PersonalFinanceAgent` + 底层金融团队 Workflow”的私人金融助手体验：持仓监控、私人观察池、风险提醒、推荐建议、决策日志、长期记忆和复盘都应进入原型。

原型中的记忆展示以 Finance Memory 为准，也就是可追溯的历史建议、用户反馈、观察理由和复盘摘要；Hermes-agent 自带记忆只作为通用上下文，不作为金融证据直接展示。

## 当前 Penpot 文件

- Penpot Web：`http://localhost:9001`
- MCP 服务：`http://localhost:4401/mcp`
- 当前项目/文件：`finance-agnet`

## 已生成页面

- `00 Design System`
  - 金融工作台色彩、排版、组件样式、信息原则
- `01 Dashboard 总览`
  - 私人金融助手首页、持仓风险、观察池触发、今日建议、待确认动作、数据源健康、决策日志入口、复盘入口
- `02 Portfolio 持仓分析`
  - 高保真持仓分析页：持仓总览、风险优先级表格、仓位结构、风险归因、加仓/减仓/卖出/换股建议、Hermes 导出入口
- `03 Signal Lab 信号解释`
  - 高保真信号解释页：持仓标的、观察池标的和推荐候选标的的统一信号解释、指标来源、数据质量状态
- `04 Agent Report AI报告`
  - 高保真 AI 报告页：今日摘要、推荐依据、风险反驳、换股比较、资产明细、审批清单、风控检查、决策日志入口
- `05 Agent 协作进展`
  - 高保真协作可视化页：Agent 小人协作地图、Workflow 任务进度、公开讨论摘要、分歧与裁决、观察池看板、记忆档案柜、Hermes 等待确认状态

## 重新生成

运行：

```powershell
node D:\Code\aiAgents\finance-agent\scripts\penpot\create-finance-agent-prototype.cjs
```

脚本会通过 Penpot MCP 写入当前已连接的 Penpot 文件。由于 Penpot 插件切换页面是异步行为，脚本已采用“切换页面 -> 等待稳定 -> 绘制当前页”的方式，避免形状落到错误页面。对于内容较重的页面，例如 `04 Agent Report AI报告`，脚本采用多次分段提交，规避 Penpot MCP 单次 `execute_code` 30 秒任务限制。

## 校验结果

最近一次生成结果保存于：

```text
D:\Code\aiAgents\finance-agent\prototypes\penpot\last-generate-result.json
```

最近一次校验显示 6 个页面各有 1 个顶层画板：

- `00 Design System` -> `Design System / Finance Agent`
- `01 Dashboard 总览` -> `Dashboard / 总览工作台 1440`
- `02 Portfolio 持仓分析` -> `Portfolio / 持仓分析 1440`
- `03 Signal Lab 信号解释` -> `Signal Lab / 信号解释 1440`
- `04 Agent Report AI报告` -> `Agent Report / AI报告 1440`
- `05 Agent 协作进展` -> `Agent Collaboration / 协作进展 1440`

其中当前高保真程度：

- `01 Dashboard 总览`：已完成第一版高保真，后续需要升级为私人金融助手首页。
- `02 Portfolio 持仓分析`：已完成第一版高保真，后续需要补持仓监控触发、换股比较和决策日志入口。
- `03 Signal Lab 信号解释`：已完成第一版高保真，后续需要同时覆盖持仓、观察池和推荐候选标的。
- `04 Agent Report AI报告`：已完成第一版高保真，后续需要补风险反驳、换股比较、长期记忆引用和复盘结果。
- `05 Agent 协作进展`：已完成第一版高保真，后续需要从普通 Agent 协作升级为金融团队 Workflow 状态投影，并增加观察池看板和记忆档案柜。

## Agent 协作可视化边界

`05 Agent 协作进展` 展示的是用户可理解、可审计的协作摘要和 Workflow 状态，而不是模型内部原始推理。

页面应展示：

- 每个 Agent 的角色、任务、状态。
- 上层 `PersonalFinanceAgent` 当前为什么触发某个 Workflow。
- 持仓风险、观察池触发、证据更新、决策日志写入和长期记忆更新。
- 可公开讨论摘要，例如观点、证据、分歧、裁决。
- 数据刷新、信号计算、风险复核、报告生成、Hermes 输出等进度。
- 哪些结论已经达成共识，哪些仍需用户确认。

页面不应展示：

- 模型隐藏链式思考。
- 不可验证的内部自言自语。
- 没有证据来源的“AI 觉得”。

## 已知限制

当前 Penpot MCP 的 `export_shape` 在本机返回 `http error`，因此脚本暂时无法稳定自动导出 PNG。可编辑设计稿已经写入 Penpot；需要截图时可以先在 Penpot UI 中查看或导出，后续再补浏览器截图/导出链路。
