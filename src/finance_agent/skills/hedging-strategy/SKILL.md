---
name: hedging-strategy
category: portfolio
markets: [ashare, crypto]
requires_engine: null
roundtable_role: portfolio_manager
---
## 适用场景
组合风险暴露较集中、需要解释对冲思路但不生成交易指令时使用。

## 输入
只读取持仓、资产相关性、风险暴露、市场环境、可交易约束和用户风险画像。

## 解读口径
描述可降低波动的思路，例如降低同类暴露、增加低相关资产或等待确认；只输出建议，不生成真实下单。

## 禁令
不得自动下单、不得绕过用户确认、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
