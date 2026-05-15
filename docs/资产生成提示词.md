# AI 生图提示词与资产生产流程

更新时间：2026-05-13  
适用范围：金融 Agent 办公室原创背景、角色、动作、特效

本文用于指导用 AI 生图工具生产 `apps/agent-office` 所需资产。目标是让资产能被 Phaser 稳定使用，而不是只生成一张好看的概念图。

## 1. 总体原则

- 先生成背景，再生成角色，再生成动作，再生成特效。
- 背景中不要包含角色。
- 背景中不要包含可读文字、logo、品牌、证券公司标识。
- 角色必须独立透明背景。
- 角色动作必须保持同一个角色设计，不要每帧变脸或换衣服。
- 每批资产必须记录版本号，避免后续覆盖。
- 生成结果要经过裁切、压缩、命名和 manifest 登记后再进入前端。

## 2. 推荐资产规格

| 资产 | 尺寸 | 格式 | 说明 |
|---|---:|---|---|
| 办公室背景 | 1280x720 | webp/png | 无角色、无文字、区域清楚 |
| 前景遮挡物 | 按对象裁切 | webp/png | 桌子、屏幕、门、机柜等 |
| Agent 单帧 | 48x48 或 64x64 | png/webp | 透明背景 |
| Agent 动作 | 每动作 4-8 帧 | spritesheet webp/png | 固定帧宽 |
| 特效 | 64x64 或 128x128 | spritesheet webp/png | 数据流、风险告警、审批通过 |
| UI 小图标 | 24x24 或 32x32 | svg/png | 可优先用 lucide 图标替代 |

## 3. 背景提示词

### 3.1 白天金融作战室

```text
1280x720 pixel art isometric fintech AI agent office,
professional trading operations room,
left side data server racks and glowing data pipelines,
middle-left signal analysis desk with multi-monitor chart screens,
center round meeting table for analyst agents,
right side risk control room with alert dashboard and portfolio radar,
bottom-right approval gate and order draft terminal,
bottom-left quiet lounge area for off-duty agents,
dark graphite professional palette,
cyan data lights, amber confirmation lights, red risk accents, green approval accents,
clean readable composition, wide walkable paths,
no characters, no text, no logos, no brand names
```

负面提示词：

```text
readable text, logo, watermark, brand name, real stock broker name,
characters, people, animals, cute bedroom, wooden cabin, snow mountain,
purple gradient background, cyberpunk nightclub, messy UI, blurry details,
overcrowded room, unreadable layout
```

### 3.2 闭市/下班状态

```text
1280x720 pixel art isometric fintech AI agent office at market closed,
same layout as a professional trading operations room,
dimmed trading screens, low brightness server lights,
most desks quiet, lounge area softly lit,
one small crypto watch terminal still glowing,
calm night audit mood,
dark graphite and deep blue palette,
subtle cyan data lights, very limited amber status lights,
no characters, no text, no logos, no brand names
```

负面提示词：

```text
readable text, logo, brand, people, characters, bright daylight,
party atmosphere, neon nightclub, messy wires everywhere,
overly cute game room, fantasy tavern
```

### 3.3 风险事件状态

```text
1280x720 pixel art isometric fintech AI agent office during a risk incident,
professional trading operations room,
risk control room on the right with red warning glow,
approval gate temporarily locked,
data server area still stable,
center meeting table ready for urgent review,
dark graphite palette, controlled red alert accents, cyan data lights,
clear walkable paths, readable room zones,
no characters, no text, no logos, no brand names
```

负面提示词：

```text
explosion, fire, chaos, horror, disaster, readable text,
logo, brand, characters, overdone red screen, cyberpunk club,
blurry, cluttered, low resolution
```

## 4. 角色设定图提示词

先生成角色设定图，不急着生成 spritesheet。设定图用于确认轮廓、颜色和道具。

### 4.1 数据管家

```text
pixel art fintech AI agent character concept,
data steward, calm and meticulous personality,
small professional office character,
blue badge, compact data terminal backpack, tiny cable spool,
dark graphite uniform with cyan details,
clear silhouette, friendly but serious,
front view, side view, back view,
transparent background,
no text, no logo, no watermark
```

负面提示词：

```text
realistic human, anime portrait, oversized weapon, fantasy armor,
readable text, logo, brand, blurry, inconsistent views,
animal features, childish costume
```

### 4.2 风险官

```text
pixel art fintech AI agent character concept,
risk officer, calm conservative personality,
small professional office character,
red warning badge, clipboard, tiny alert light accessory,
dark graphite suit with controlled red accents,
clear silhouette, authoritative but not aggressive,
front view, side view, back view,
transparent background,
no text, no logo, no watermark
```

负面提示词：

```text
police uniform, military armor, fantasy knight, angry villain,
readable text, logo, brand, photorealistic, blurry,
animal features, childish costume
```

### 4.3 信号分析师

```text
pixel art fintech AI agent character concept,
signal analyst, fast technical personality,
small professional office character,
green jacket accents, multi-screen glasses, small chart badge,
dark office outfit, clean silhouette,
front view, side view, back view,
transparent background,
no text, no logo, no watermark
```

### 4.4 研究员

```text
pixel art fintech AI agent character concept,
research analyst, evidence-focused personality,
small professional office character,
gold notebook, document folder, sticky note accessory,
dark office outfit with amber and gold details,
clear silhouette, thoughtful posture,
front view, side view, back view,
transparent background,
no text, no logo, no watermark
```

### 4.5 草案员

```text
pixel art fintech AI agent character concept,
order draft specialist, careful workflow personality,
small professional office character,
purple approval folder, small printed draft paper accessory,
dark office outfit with subtle purple accents,
clear silhouette, procedural and precise,
front view, side view, back view,
transparent background,
no text, no logo, no watermark
```

### 4.6 总协调员

```text
pixel art fintech AI agent character concept,
chief coordinator, warm decisive personality,
small professional office character,
cyan leadership badge, meeting note board,
dark office outfit with blue-cyan accents,
clear silhouette, calm and trustworthy,
front view, side view, back view,
transparent background,
no text, no logo, no watermark
```

## 5. 角色动作提示词

角色动作必须基于已确认的设定图。每次生成时把设定图作为 reference image。

### 5.1 通用 idle 动作

```text
pixel art character sprite sheet,
same character as reference image,
48x48 frame size, 4 frames,
idle breathing animation,
front-facing small office character,
transparent background,
consistent outfit, consistent face, consistent colors,
no text, no logo, no watermark
```

### 5.2 通用 walk 动作

```text
pixel art character sprite sheet,
same character as reference image,
48x48 frame size, 8 frames,
side-view walk cycle,
small professional office character,
transparent background,
consistent outfit, consistent face, consistent colors,
clean readable silhouette,
no text, no logo, no watermark
```

### 5.3 数据管家 working

```text
pixel art character sprite sheet,
same data steward character as reference image,
48x48 frame size, 6 frames,
working animation, checking data terminal backpack,
holding a small glowing data packet,
cyan data light effect,
transparent background,
consistent character design,
no text, no logo, no watermark
```

### 5.4 风险官 working / blocked

```text
pixel art character sprite sheet,
same risk officer character as reference image,
48x48 frame size, 6 frames,
risk review animation,
holding clipboard and raising a small warning sign shape without text,
controlled red alert light effect,
transparent background,
consistent character design,
no text, no logo, no watermark
```

### 5.5 offDuty 动作

```text
pixel art character sprite sheet,
same character as reference image,
48x48 frame size, 4 frames,
off duty resting animation,
small professional office character sitting quietly or sleepy standing pose,
low brightness mood,
transparent background,
consistent outfit and colors,
no text, no logo, no watermark
```

## 6. 特效提示词

### 6.1 数据流

```text
pixel art effect sprite sheet,
64x64 frame size, 8 frames,
cyan data stream particles moving along a short pipeline,
transparent background,
clean subtle professional effect,
no text, no logo
```

### 6.2 风险告警

```text
pixel art effect sprite sheet,
64x64 frame size, 8 frames,
controlled red warning pulse,
small alert ring and exclamation shape without text,
transparent background,
professional fintech risk alert,
not too bright,
no text, no logo
```

### 6.3 审批通过

```text
pixel art effect sprite sheet,
64x64 frame size, 8 frames,
green approval pulse and small check shape,
transparent background,
subtle professional confirmation effect,
no text, no logo
```

### 6.4 信号卡生成

```text
pixel art effect sprite sheet,
64x64 frame size, 8 frames,
small glowing signal card appears with chart-like abstract lines,
green and cyan accents,
transparent background,
no readable text, no logo
```

## 7. 生成顺序

推荐顺序：

1. 生成白天背景 4 张候选。
2. 挑 1 张作为基准布局。
3. 基于基准布局生成闭市版和风险事件版。
4. 生成数据管家设定图 4 张候选。
5. 生成风险官设定图 4 张候选。
6. 确认两个角色后生成 idle / walk / working / offDuty。
7. 生成数据流、风险告警、审批通过、信号卡特效。
8. 使用脚本裁切 spritesheet。
9. 压缩 webp。
10. 登记到 `assetsManifest.ts`。
11. 在 Phaser 中验证尺寸、锚点、动画速度和图层遮挡。

## 8. 资产处理流程

### 8.1 背景处理

检查项：

- 是否有乱码文字。
- 是否有 logo 或品牌。
- 通道是否能让 48x48/64x64 角色通过。
- 六大区域是否清晰。
- 是否有明显前景遮挡物需要单独裁切。
- 是否需要修掉不合理图形。

导出：

```text
apps/agent-office/public/assets/office/backgrounds/office_fintech_warroom_day_v001.webp
apps/agent-office/public/assets/office/backgrounds/office_fintech_warroom_closed_v001.webp
apps/agent-office/public/assets/office/backgrounds/office_fintech_warroom_incident_v001.webp
```

### 8.2 角色处理

检查项：

- 每帧是否同一个角色。
- 脚底位置是否稳定。
- 透明背景是否干净。
- 帧尺寸是否统一。
- 朝向是否满足场景需要。
- 小尺寸下能否看出身份。

导出：

```text
apps/agent-office/public/assets/office/agents/agent_data_steward_idle_v001.webp
apps/agent-office/public/assets/office/agents/agent_data_steward_walk_v001.webp
apps/agent-office/public/assets/office/agents/agent_data_steward_working_v001.webp
apps/agent-office/public/assets/office/agents/agent_data_steward_offduty_v001.webp
```

### 8.3 特效处理

检查项：

- 是否过亮。
- 是否遮挡 Agent。
- 是否循环自然。
- 是否支持透明背景。
- 是否能在深色背景上看清。

导出：

```text
apps/agent-office/public/assets/office/effects/fx_data_stream_v001.webp
apps/agent-office/public/assets/office/effects/fx_risk_alert_v001.webp
apps/agent-office/public/assets/office/effects/fx_approval_pass_v001.webp
apps/agent-office/public/assets/office/effects/fx_signal_card_v001.webp
```

## 9. Phaser 导入约定

建议每个 spritesheet 配置：

```ts
{
  key: "agent-data-steward-walk",
  path: "/assets/office/agents/agent_data_steward_walk_v001.webp",
  frameWidth: 48,
  frameHeight: 48,
  frameRate: 8,
  repeat: -1
}
```

角色锚点：

```text
originX: 0.5
originY: 1.0
```

原因：

- 以脚底为定位点，方便路线规划和遮挡排序。

## 10. 最小资产清单

第一版只需要：

```text
backgrounds/
  office_fintech_warroom_day_v001.webp
  office_fintech_warroom_closed_v001.webp
  office_fintech_warroom_incident_v001.webp

agents/
  agent_data_steward_idle_v001.webp
  agent_data_steward_walk_v001.webp
  agent_data_steward_working_v001.webp
  agent_data_steward_offduty_v001.webp
  agent_risk_officer_idle_v001.webp
  agent_risk_officer_walk_v001.webp
  agent_risk_officer_working_v001.webp
  agent_risk_officer_offduty_v001.webp

effects/
  fx_data_stream_v001.webp
  fx_risk_alert_v001.webp
  fx_approval_pass_v001.webp
```

## 11. 失败样例判断

以下结果不能进入项目：

- 背景里有不可控乱码文字。
- 背景里有角色。
- 角色每帧长得不一样。
- 角色动作帧尺寸不一致。
- 风险告警过亮导致看不清 UI。
- 画面像科幻夜店，而不是金融办公室。
- 画面像普通居家小屋，而不是数据/交易/风控工作场景。
- 资产只有概念图，不能拆成 Phaser 可用素材。

## 12. 后续可自动化脚本

后续可以补脚本：

```text
scripts/assets/slice_spritesheet.py
scripts/assets/convert_to_webp.py
scripts/assets/check_transparency.py
scripts/assets/generate_manifest.py
```

脚本职责：

- 裁切 spritesheet。
- 批量转 webp。
- 检查透明背景。
- 自动生成 `assetsManifest.ts`。
- 输出资产体积报告。

