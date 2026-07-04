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

## 禁令
不得自己画 XABCD、不得自己计算斐波那契比例、不得声称 pyharmonics 已启用、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
