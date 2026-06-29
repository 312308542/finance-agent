---
name: pair-trading
category: portfolio
markets: [ashare, crypto]
requires_engine: pair_trading
roundtable_role: portfolio_manager
---
## 适用场景
配对/协整引擎已输出价差、z-score、半衰期和显著性时，用于解释相对价值机会。

## 输入
只读取配对引擎输出、两端资产基本信息、相关风险和组合约束。

## 解读口径
先说明配对关系是否稳健，再解释价差偏离与回归概率，最后提示模型失效和流动性风险。

## 禁令
不得自己计算协整或价差、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
