---
name: seasonal
category: technical
markets: [ashare, crypto]
requires_engine: seasonal
roundtable_role: technical_analyst
---
## 适用场景
季节性引擎已经输出月份/星期等周期收益画像时，用于解释是否存在稳定的时间窗口偏差。

## 接入状态
接口预留：确定性适配器已实现，但生产调度和明确消费场景尚未建立；capability 保持 False，不得默认加载。

## 输入
只读取 seasonal 引擎输出的周期收益均值、样本数、胜率、最佳/最差周期和 evidence_id。

## 解读口径
先看样本数是否足够；再说明季节性是否稳定；最后把季节性作为弱辅助，不得覆盖趋势、基本面或风险信号。

## 禁令
不得自己计算季节性、不得把季节性当作确定买卖信号、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
