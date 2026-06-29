---
name: candlestick
category: technical
markets: [ashare, crypto]
requires_engine: talib_cdl
roundtable_role: technical_analyst
---
## 适用场景
K 线形态已经由 ta-lib CDL 类指标识别后，对形态含义进行解释。

## 输入
只读取确定性形态输出、K 线位置、成交量和邻近支撑压力。

## 解读口径
形态必须结合趋势位置和量能确认，不能把单根 K 线当成独立买卖依据。

## 禁令
不得自己计算形态、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
