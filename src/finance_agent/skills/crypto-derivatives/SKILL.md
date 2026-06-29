---
name: crypto-derivatives
category: flow
markets: [crypto]
requires_engine: null
roundtable_role: flow_analyst
---
## 适用场景
数字货币资金费率、未平仓量、多空比和合约拥挤度解读。

## 输入
只读取已入库衍生品快照、价格趋势、成交量和清算风险。

## 解读口径
结合价格方向判断杠杆是否顺势拥挤，拥挤时提示反向波动风险。

## 禁令
不得自己抓取交易所数据、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
