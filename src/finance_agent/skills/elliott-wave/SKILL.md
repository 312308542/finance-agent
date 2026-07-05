---
name: elliott-wave
category: technical
markets: [ashare, crypto]
requires_engine: structural_lite_elliott
roundtable_role: technical_analyst
---
## 适用场景
本项目 structural-lite / elliott_lite_v2 引擎已经输出候选浪型、置信度、确认价和失效价时，用于解释趋势阶段和主观性风险。

## 输入
只读取 structural-lite 的 elliott_lite_v2 输出，包括浪型候选、置信度、输入周期、结构状态、`thesis_confirmation_price`、`thesis_invalidation_price`、`confirmed_at`、evidence_id 和数据质量。

## 解读口径
先说明浪型置信度是否足够；低置信度或 `insufficient_structure` 时只提示“结构不清晰”；高置信度时解释候选主浪、调整浪、信号提示、`thesis_confirmation_price`（衰竭假设被确认的价格）和 `thesis_invalidation_price`（衰竭假设失效的价格）。波浪计数主观性高，只能作为技术结构补充证据。

解读推动浪候选时，把 5 浪结构表述为“顺趋势方向的候选路径”：浪 1 是趋势启动，浪 2 是回撤确认，浪 3 通常代表最强趋势段，浪 4 是浅回调或横向整理，浪 5 是末端推进并可能伴随背离。只能基于引擎输出的候选与置信度说明阶段，不得扩展出未入库的浪段。

解读调整浪候选时，把 ABC 结构表述为“逆趋势修正候选”：A 浪是初始回调，B 浪是反弹或回抽，C 浪是第二段修正。若引擎 payload 给出 zigzag/flat/triangle 等类型，仅解释其对趋势连续性的含义，不把调整浪当成确定反转。

解读 Fibonacci 关系时，只引用引擎已算出的比例或校验结论：浪 2 常见回撤区间约 0.5/0.618，过深会削弱推动浪假设；浪 3 与浪 1 的扩展关系常用于判断趋势强度；浪 4 通常较浅；浪 5 与浪 1 的相似长度或衰竭失败，只能作为候选结构质量说明。

## 失效条件
推动浪候选的硬性失效条件包括：浪2不破浪1起点、浪3不是最短、浪4不进入浪1区域。若引擎输出显示任何一条被破坏，技术分析师只能说明“推动浪假设失效或需降级”，不能继续按原浪型解释。

当 `thesis_invalidation_price` 存在时，它是当前衰竭/候选浪型假设的优先失效锚点；当 `thesis_confirmation_price` 存在时，它是衰竭假设需要被市场确认的条件。价格未触发确认前，只能表述为候选而非结论。

若 ABC 候选中 B 浪回撤过深、C 浪力度与 A 浪关系不成立，或候选置信度低于引擎阈值，应提示“结构不清晰”，并回到趋势、风险和数据质量上下文综合解读。

## 禁令
不得自己数浪、不得在低置信度时强行输出波浪观点、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。

## 来源
来源：Vibe-Trading references，MIT，已按只读解读视角改写。
