---
name: harmonic
category: technical
markets: [ashare, crypto]
requires_engine: harmonic
roundtable_role: technical_analyst
---
## 适用场景
本项目 structural-lite / harmonic_lite_v2 引擎已经输出 XABCD 点位、形态类型、四比例校验、完成度、置信度和失效条件时，用于解释潜在反转结构。

## 输入
只读取 structural-lite 的 harmonic_lite_v2 输出，包括形态类型、XABCD 点位、`b_retrace`、`d_retrace`、`bc_ratio`、`cd_ratio`、fit 分数、`bars_since_d`、完成度、置信度、失效条件、输入区间、`confirmed_at` 和 evidence_id。

## 解读口径
先确认形态是否完成；再解释比例是否合格、风险是否可控；最后说明该形态与趋势、波动和系统评分的冲突。谐波形态只能作为潜在反转候选，不得把候选形态解释成确定买卖动作。

解读 XABCD 时，只说明引擎已经识别出的点位、方向、形态类型和比例校验：Gartley 的关键是 D 点接近 XA 的 0.786 回撤；Bat 的 B 点回撤较浅、D 点更深；Butterfly 的 D 点通常超出 X 点；Crab 的 D 点延伸最远。若引擎未给出对应形态，不得自行补画。

解读比例时，优先看 `b_retrace`、`d_retrace`、`bc_ratio`、`cd_ratio` 和 fit 分数是否同时支持形态；四比例越一致，形态解释力越强；只满足单个比例时应提示结构证据不足。

解读 PRZ（潜在反转区）时，把它表述为“多个 Fibonacci 关系汇聚后的候选反应区”，而不是必然反转区。D 点完成后仍需要结合趋势、波动、量价或风险反驳判断是否有冲突；高级别周期形态通常比低级别周期更值得关注，但仍不能替代系统评分。

## 失效条件
谐波候选的基础失效条件是引擎给出的 `invalidation_price` 被触发；看涨形态通常在 D 点下方失效，看跌形态通常在 D 点上方失效，具体以引擎输出为准。

若 D 点距离当前时间过久（例如 `bars_since_d` 超出引擎窗口）、形态完成度不足、四比例校验不再满足，或价格穿越 PRZ 后没有任何反应，应把形态降级为陈旧或无效候选。

若趋势、结构证据或风险反驳与谐波方向相反，应明确说明“潜在反转结构与主趋势冲突”，不得把谐波候选解释成独立的确定动作。

## 禁令
不得自己画 XABCD、不得自己计算斐波那契比例、不得声称 pyharmonics 已启用、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。

## 来源
来源：Vibe-Trading references，MIT，已按只读解读视角改写。
