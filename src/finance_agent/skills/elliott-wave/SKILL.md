---
name: elliott-wave
category: technical
markets: [ashare, crypto]
requires_engine: elliott_wave
roundtable_role: technical_analyst
---
## 适用场景
本项目 structural-lite / elliott_lite_v2 引擎已经输出候选浪型、置信度、确认价和失效价时，用于解释趋势阶段和主观性风险。

## 输入
只读取 structural-lite 的 elliott_lite_v2 输出，包括浪型候选、置信度、输入周期、结构状态、`thesis_confirmation_price`、`thesis_invalidation_price`、`confirmed_at`、evidence_id 和数据质量。

## 解读口径
先说明浪型置信度是否足够；低置信度或 `insufficient_structure` 时只提示“结构不清晰”；高置信度时解释候选主浪、调整浪、信号提示、`thesis_confirmation_price`（衰竭假设被确认的价格）和 `thesis_invalidation_price`（衰竭假设失效的价格）。波浪计数主观性高，只能作为技术结构补充证据。

## 禁令
不得自己数浪、不得在低置信度时强行输出波浪观点、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
