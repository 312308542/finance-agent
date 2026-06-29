---
name: correlation-analysis
category: portfolio
markets: [ashare, crypto]
requires_engine: correlation
roundtable_role: portfolio_manager
---
## 适用场景
相关矩阵和滚动相关性已由确定性引擎输出时，用于解释组合集中度和风险传导。

## 输入
只读取相关性引擎输出、资产分组、组合权重和风险上下文。

## 解读口径
说明高相关资产是否造成同涨同跌，相关性是否近期升高，以及是否需要降低同类暴露。

## 禁令
不得自己计算相关矩阵、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
