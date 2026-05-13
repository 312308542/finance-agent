# Penpot 原型说明

本目录用于记录 Hermes Finance Agent 在 Penpot 中生成的可编辑原型产物。

## 当前 Penpot 文件

- Penpot Web：`http://localhost:9001`
- MCP 服务：`http://localhost:4401/mcp`
- 当前项目/文件：`finance-agnet`

## 已生成页面

- `00 Design System`
  - 金融工作台色彩、排版、组件样式、信息原则
- `01 Dashboard 总览`
  - AI 今日结论、组合风险雷达、Agent 运行状态、市场概览、持仓诊断、观察清单、信号共识、操作草案
- `02 Portfolio 持仓分析`
  - 高保真持仓分析页：持仓总览、风险优先级表格、仓位结构、风险归因、调仓草案、Hermes 导出入口
- `03 Signal Lab 信号解释`
  - 高保真信号解释页：信号流水线、多资产信号矩阵、单资产解释、指标来源、数据质量状态
- `04 Agent Report AI报告`
  - 高保真 AI 报告页：今日摘要、推荐依据、反例、资产明细、审批清单、风控检查、草案批准/复核入口
- `05 Agent 协作进展`
  - 高保真协作可视化页：Agent 小人协作地图、任务进度、公开讨论摘要、分歧与裁决、决策轨迹、Hermes 等待确认状态

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

- `01 Dashboard 总览`：已完成第一版高保真。
- `02 Portfolio 持仓分析`：已完成第一版高保真，重点验证组合风险解释和订单草案交互。
- `03 Signal Lab 信号解释`：已完成第一版高保真，重点验证成熟指标库到统一信号协议的解释链路。
- `04 Agent Report AI报告`：已完成第一版高保真，重点验证 AI 结论阅读、证据链、反例提示、订单草案审批体验。
- `05 Agent 协作进展`：已完成第一版高保真，重点验证用户能否看到 Agent 协作进度、公开讨论摘要、分歧裁决和决策轨迹。

## Agent 协作可视化边界

`05 Agent 协作进展` 展示的是用户可理解、可审计的协作摘要，而不是模型内部原始推理。

页面应展示：

- 每个 Agent 的角色、任务、状态。
- 可公开讨论摘要，例如观点、证据、分歧、裁决。
- 数据刷新、信号计算、风险复核、报告生成、Hermes 输出等进度。
- 哪些结论已经达成共识，哪些仍需用户确认。

页面不应展示：

- 模型隐藏链式思考。
- 不可验证的内部自言自语。
- 没有证据来源的“AI 觉得”。

## 已知限制

当前 Penpot MCP 的 `export_shape` 在本机返回 `http error`，因此脚本暂时无法稳定自动导出 PNG。可编辑设计稿已经写入 Penpot；需要截图时可以先在 Penpot UI 中查看或导出，后续再补浏览器截图/导出链路。
