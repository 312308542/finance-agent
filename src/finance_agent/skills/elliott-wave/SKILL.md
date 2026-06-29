---
name: elliott-wave
category: technical
markets: [ashare, crypto]
requires_engine: elliott_wave
roundtable_role: technical_analyst
---
## 适用场景
波浪计数引擎已经输出候选浪型、置信度和失效条件时，用于解释趋势阶段和主观性风险。

## 输入
只读取 elliott_wave 引擎输出的浪型候选、置信度、输入周期、失效位、evidence_id 和数据质量。

## 解读口径
先说明浪型置信度是否足够；低置信度时只提示“结构不清晰”；高置信度时解释主浪、调整浪和失效条件。

## 禁令
不得自己数浪、不得在低置信度时强行输出波浪观点、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
