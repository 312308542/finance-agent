# Hermes Agent Office 经营类原型

这是金融 Agent 系统的可交互前端原型。当前版本已经从纯 React/CSS 小人动画重构为 `React + Phaser`：

- React 负责金融信息面板、AI 讨论、审批、证据链抽屉。
- Phaser 负责经营类游戏办公室场景、小人移动、工位、圆桌、任务卡流转。
- `officeGameBus` 负责 React 和 Phaser 之间的事件桥接。

## 目录

```text
src/
  data/officeData.tsx          # Agent、阶段、讨论、草案等领域数据
  game/assetsManifest.ts       # 资产清单，后续可直接切到原创素材
  game/agentProfiles.ts        # Agent 人设与动作能力定义
  game/eventBus.ts             # React 与 Phaser 通信事件
  game/FinanceOfficeScene.ts   # Phaser 办公室场景
  game/officeMapConfig.ts      # 色板、任务流、经营 HUD 配置
  game/PhaserOffice.tsx        # React 内嵌 Phaser 容器
  game/routePlanner.ts         # 基础 waypoint 路线规划
  game/worldConfig.ts          # 世界尺寸、交互区、导航点、工位配置
  game/worldState.ts           # 世界时段与运行状态
  main.tsx                     # React 页面与金融面板
  styles.css                   # React 面板与画布布局样式
```

## 当前架构

当前原型采用“金融工作台 + 经营类办公室”的双层结构：

- React 展示严肃金融信息，包括结论、持仓动作草案、Agent 公开讨论摘要、分歧裁决和证据链。
- Phaser 展示协作进度，包括真实办公室背景、像素小人移动、任务卡流转、工位高亮、审批门状态和经营 HUD。
- `worldConfig.ts`、`routePlanner.ts`、`assetsManifest.ts` 已把世界、路线和素材入口拆开，后续可以逐步替换为 Tiled 地图、Aseprite/TexturePacker 精灵图和真实任务事件。
- 页面只展示用户可审计的 Agent 协作摘要，不展示模型隐藏推理过程。

## Star-Office-UI 参考

当前原型参考了 `D:\Code\aiAgents\Star-Office-UI` 的体验方式：

- 使用 1280×720 像素办公室作为完整舞台，而不是把房间切成普通信息卡。
- 使用 ArkPixel 字体、硬边像素面板、底部状态控制台和右侧抽屉。
- 使用 Phaser 资源预加载、精灵帧动画、区域热点和状态驱动动画。
- `public/static` 中的 Star 资产仅用于本地原型参考；正式产品需要替换为自有原创或已授权素材。

## 运行

```powershell
cd D:\Code\aiAgents\finance-agent\apps\agent-office
npm install
npm run dev -- --port 5177
```

默认地址：

```text
http://localhost:5177
```

## 设计边界

页面展示的是用户可理解、可审计的协作摘要，不展示模型隐藏推理。后续接入真实系统时，建议 Hermes/MCP 通过 SSE 或 WebSocket 推送这些事件：

- `agent.status.changed`
- `agent.message.summary`
- `task.progress.changed`
- `evidence.updated`
- `order_draft.created`
- `approval.changed`

## 后续演进

下一阶段建议：

- 接入 Tiled 地图和像素资产包，把当前程序化绘制的办公室替换为可编辑地图与专业素材。
- 用 SSE 或 WebSocket 接入真实 Agent 运行事件，让小人状态、任务卡和右侧讨论流实时变化。
- 为证据链抽屉接入真实 `SignalSnapshot`、`RiskReview`、`RecommendationReport`、`OrderDraft` 数据。
- 将当前静态经营 HUD 改成由后端事件驱动的数据新鲜度、信号批次、风控拦截和报告置信度。
