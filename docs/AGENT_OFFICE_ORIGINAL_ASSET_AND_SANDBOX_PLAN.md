# 金融 Agent 办公室原创资产与沙盒体验方案

本文记录当前关于 `Agent 经营办公室` 原型的讨论结论、问题判断、原创资产路线和后续实现路径。

## 1. 当前问题判断

当前 `apps/agent-office` 已经实现了 `React + Phaser` 原型，也参考了 `D:\Code\aiAgents\Star-Office-UI` 的体验方式。但直接借用 Star 风格资产后，出现了几个明显问题：

- 画面风格不匹配金融场景：雪山小屋/生活化办公室不适合表达股票、数字货币、风控和订单草案。
- 信息层和背景层打架：区域框、路径线、气泡、HUD、任务卡同时覆盖在背景上，会显得像调试模式。
- Agent 只是“会动的小人”，还没有稳定人格、外貌和动作体系。
- 背景只是一张图，没有真正的碰撞、可走区域、工位、路线和上下班状态。
- 用户想要的是“AI 金融办公室自动运转，自己旁观并确认结果”，不是普通网页里嵌一段动画。

结论：下一阶段不能只换背景图，而要把它当作一个轻量 2D 沙盒世界来设计。

## 2. 目标体验

目标是构建一个“金融 Agent 作战办公室”：

- 用户打开页面后，第一眼看到一个完整的金融办公室世界。
- Agent 会根据真实任务状态自动移动、工作、讨论、复核、休息。
- 用户不需要理解量化细节，只看结论、风险、证据链和是否需要确认。
- 页面展示的是可审计协作摘要，不展示模型隐藏推理。
- AI/金融系统驱动事件，Phaser 只负责世界呈现。

理想体验不是“用户玩游戏”，而是“用户看见 AI 团队在办公室里替自己工作”。

## 3. 总体架构

建议拆成三层：

```text
美术设定层
  Agent 人设、外貌、动作、办公室区域、色彩、资产规格

世界规则层
  地图、碰撞、可走区域、路线、上下班、任务状态机、交互热点

金融业务层
  行情刷新、信号计算、风控复核、报告生成、订单草案、用户审批
```

Phaser 只消费“意图事件”，不直接理解金融逻辑。

示例：

```json
{
  "type": "risk.reviewing",
  "agent": "risk",
  "targetZone": "riskRoom",
  "message": "正在复核新能源仓位风险"
}
```

Phaser 收到事件后执行：

```text
风险官移动到风控室 -> 播放 working 动画 -> 显示轻量任务卡 -> 更新底部面板
```

## 4. Agent 人设、外貌与动作

每个 Agent 需要一份稳定的 `AgentProfile`，包括人格、职责、外观、动作、语气和工作习惯。

| Agent | 人格特征 | 外貌方向 | 核心动作 |
|---|---|---|---|
| 数据管家 | 严谨、安静、强迫症式校验 | 蓝色工牌、数据终端背包、像 API 管道管理员 | 拉数据、检查表格、举起行情包、标记缺失值 |
| 信号分析师 | 快节奏、技术派、相信指标但接受反驳 | 绿色外套、多屏眼镜、图表徽章 | 看图表、生成信号卡、对比指标、摇头否定 |
| 风险官 | 冷静、保守、会打断别人 | 红色警戒徽章、夹板、警报灯元素 | 举牌拦截、拉警报、标红风险、阻止草案 |
| 研究员 | 慢热、证据派、偏基本面 | 金色笔记本、资料夹、便签 | 翻资料、贴便签、阅读新闻、补充证据 |
| 草案员 | 执行型、流程控、等待确认 | 紫色审批夹、打印机元素 | 打印草案、递交订单、等待签字 |
| 总协调员 | 温和但决断，负责裁决 | 蓝色领队标识、会议记录板 | 召集会议、裁决分歧、发布报告 |

### 动作状态

每个 Agent 至少需要以下动作：

```text
idle        待命
walk        移动
working     工作中
thinking    分析中
arguing     讨论/分歧
blocked     被风控拦截
approved    审批通过
offDuty     下班/休眠
```

第一阶段不必全部完成，建议最小动作集：

```text
idle
walk
working
offDuty
```

### 资产规格建议

```text
角色尺寸：48x48 或 64x64
每个动作：4-8 帧
每个 Agent：idle / walk / working / offDuty 起步
导出格式：spritesheet.webp + atlas.json 或统一帧宽 spritesheet.webp
```

## 5. 金融办公室背景设计

背景不应再是生活化小屋，而应是“像素风金融 Agent 作战室”。

建议区域布局：

```text
左侧：数据机房 / 行情屏 / API 数据管道
中左：信号分析台 / 多屏图表 / 指标计算台
右侧：风控室 / 警报屏 / 仓位雷达
中下：圆桌会议 / Agent 讨论区
右下：审批门 / Hermes 出单闸口
左下：休息区 / 下班待命区
```

背景生成原则：

- 画面中不要生成文字，所有文字由前端渲染。
- 不要生成具体品牌、logo、证券公司标识。
- 保持 1280×720，构图要清晰，区域边界明确。
- 角色不要直接画在背景里，角色需要独立 sprite。
- 背景要预留 Agent 站位和移动通道。

## 6. 背景、交互和碰撞

背景不能只是一张死图。即使先用 AI 生图，也要在代码中维护地图配置。

建议地图层：

```text
background       固定背景
foreground       前景遮挡物，如桌子、柱子、屏幕
collision        不可走区域
walkable         可走区域
interactables    可点击对象
spawnPoints      Agent 出生点
workstations     Agent 工位
navPoints        路线节点
```

第一阶段可以不用 Tiled，先用 TypeScript 配置：

```ts
export const officeWorld = {
  stage: { width: 1280, height: 720 },
  workstations: {
    dataDesk: { x: 190, y: 250 },
    signalDesk: { x: 460, y: 280 },
    riskRoom: { x: 980, y: 235 },
    meetingTable: { x: 650, y: 460 },
    approvalGate: { x: 1050, y: 520 },
    lounge: { x: 160, y: 560 },
  },
  blockedAreas: [
    { x: 0, y: 0, w: 1280, h: 90 },
    { x: 520, y: 170, w: 210, h: 120 },
  ],
  interactables: [
    { id: "dataDesk", label: "数据台", targetAgent: "data" },
    { id: "riskRoom", label: "风控室", targetAgent: "risk" },
  ],
};
```

后续更正式时再接入 Tiled：

```text
Tiled Object Layer
  collision polygons
  interactable zones
  workstation points
  navigation nodes
```

## 7. 人物移动路线规划

当前直接 tween 到目标点不够自然。需要路线规划。

### 简单版

手写 waypoint 图：

```ts
routes: {
  dataDesk_to_signalDesk: [
    { x: 220, y: 300 },
    { x: 370, y: 320 },
    { x: 460, y: 280 },
  ],
  signalDesk_to_riskRoom: [
    { x: 520, y: 320 },
    { x: 760, y: 300 },
    { x: 980, y: 235 },
  ],
}
```

适合第一版，因为金融流程是固定路径：

```text
数据刷新 -> 信号计算 -> 风控复核 -> 圆桌讨论 -> 审批确认
```

### 正式版

使用 navmesh 或网格 A*：

```text
点击任意可走区域 -> 查询可走网格 -> 生成路径 -> 逐点移动
```

正式版适合后续做类沙盒体验。

## 8. 未分析时段和下班状态

金融系统不是一直处于分析中。需要世界状态表达“未分析时段”。

建议状态：

```text
marketClosed      A 股闭市，Agent 低频巡检
cryptoWatch       Crypto 继续值班
offDuty           大多数 Agent 离开工位
nightAudit        夜间批处理
idleOffice        没有任务，办公室待命
incidentMode      风险异常，风控 Agent 被唤醒
```

视觉表现：

- 灯光变暗。
- 大屏进入低亮度。
- 大多数 Agent 回休息区或离线座位。
- 只保留值班 Agent，例如 Crypto 值班员。
- 顶部状态显示“等待下一次数据刷新”。
- 夜间批处理时，数据机房和日志屏微亮。

示例：

```json
{
  "type": "world.clock.changed",
  "session": "marketClosed",
  "visibleAgents": ["data", "risk"],
  "message": "A 股已闭市，Crypto 值班中"
}
```

## 9. 类沙盒世界体验

沙盒体验的关键不是让用户手动控制小人，而是让办公室自动运转。

建议系统：

```text
WorldClock
  控制交易时段、夜间、休息、异常模式

AgentStateMachine
  控制每个 Agent 的 idle / walk / working / offDuty

TaskQueue
  接收金融系统任务，排队执行

RoutePlanner
  根据目标工位生成移动路径

InteractionSystem
  处理点击 Agent、工位、任务卡、审批门

EventBus
  对接 Hermes/MCP/后端 Agent 事件
```

用户可交互点：

- 点击 Agent：打开该 Agent 的证据链和当前任务。
- 点击工位：查看该区域当前数据。
- 点击任务卡：查看任务进度、输入、输出、证据。
- 点击审批门：查看订单草案并确认或拒绝。
- 切换时间：查看白天、闭市、夜间审计状态。

## 10. 只有 AI 生图工具时的实现方法

只有 AI 生图工具也能做第一版，但要采用“资产底稿 + 工程切分”的流程。

### 背景

背景最适合 AI 生成。

提示词原则：

```text
1280x720 pixel art isometric fintech AI agent office,
trading terminals, data server racks, risk control room,
round-table analyst meeting area, approval gate,
dark professional palette, gold/cyan/red signal accents,
clean readable composition,
no text, no logos, no characters
```

生成后处理：

- 去掉 AI 生成的乱码文字。
- 检查通道是否足够宽。
- 确认每个区域都有清楚站位。
- 压缩为 `webp`。

### 角色

角色不能一次生成六个 Agent 全动作。需要分步：

1. 每个 Agent 先生成角色设定图。
2. 基于设定图生成 `idle / walk / working / offDuty` 动作。
3. 每个动作 4-8 帧。
4. 用脚本裁切成 spritesheet。
5. Phaser 按帧播放。

角色提示词模板：

```text
pixel art character sprite sheet, 48x48 frame,
fintech AI data steward, blue badge, tiny data terminal backpack,
front view and side walk cycle, clean silhouette,
transparent background, consistent character design,
no text, no logo
```

### 特效

可先生成或手写简单特效：

```text
数据流光效
信号卡片
风控红色警报
审批通过光效
夜间低亮灯光
```

第一版可以继续用 Phaser 图形绘制特效，不必全部 AI 生成。

## 11. 最小垂直切片

建议不要一次做全量。先做一个可验收切片：

```text
1 张原创金融办公室背景
2 个 Agent：数据管家、风险官
每个 Agent：idle / walk / working / offDuty
1 个任务卡资产
1 个风控警报特效
1 条流程：数据刷新 -> 风控复核 -> 下班待命
```

验收标准：

- 不看底部文字，也能知道 Agent 在不同区域工作。
- 点击 Agent 能打开对应证据链。
- Agent 移动路线不会穿墙、穿桌子。
- 无任务时办公室能自然进入待命/下班状态。
- 画面不再像调试层，而像一个正在运行的金融办公室。

## 12. 推荐文件与模块拆分

后续建议新增：

```text
docs/ART_DIRECTION.md
  美术风格、区域设定、Agent 人设、动作规范

docs/ASSET_PROMPTS.md
  AI 生图提示词、负面词、版本记录

apps/agent-office/src/game/worldConfig.ts
  工位、碰撞、路线、可交互对象

apps/agent-office/src/game/agentProfiles.ts
  Agent 人格、外貌、动作、默认位置

apps/agent-office/src/game/routePlanner.ts
  路线规划，第一版可用手写 waypoint

apps/agent-office/src/game/worldState.ts
  交易时段、下班、夜间审计、异常模式

apps/agent-office/src/game/assetsManifest.ts
  原创背景、角色、特效资产清单
```

## 13. 后续实施路径

### 阶段 1：文档和提示词

- 输出 `ART_DIRECTION.md`。
- 输出 `ASSET_PROMPTS.md`。
- 明确 1280×720 背景区域布局。
- 明确 6 个 Agent 的人设、服装、动作。

### 阶段 2：AI 生成第一套资产

- 生成 3-5 张背景候选。
- 选 1 张做主背景。
- 生成数据管家和风险官角色设定。
- 生成 idle / walk / working / offDuty 动作。

### 阶段 3：工程接入

- 将背景接入 Phaser。
- 将 2 个 Agent 的 spritesheet 接入 Phaser。
- 新增 `worldConfig.ts`。
- 实现简单路线规划。
- 实现下班/待命状态。

### 阶段 4：扩展到完整团队

- 增加信号分析师、研究员、草案员、总协调员。
- 增加圆桌会议状态。
- 增加审批门交互。
- 增加夜间审计和 Crypto 值班。

### 阶段 5：真实业务事件接入

- 用 SSE 或 WebSocket 接入 Hermes/MCP。
- 后端推送任务事件。
- 前端只消费公开摘要和状态事件。
- 证据链抽屉接真实 `SignalSnapshot`、`RiskReview`、`RecommendationReport`、`OrderDraft`。

