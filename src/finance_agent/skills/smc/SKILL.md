---
name: smc
category: technical
markets: [ashare, crypto]
requires_engine: smc
roundtable_role: technical_analyst
---
## 适用场景
本项目 structural-lite / smc_lite_v2 引擎已经输出 BOS、CHoCH、FVG、回补状态和置信度时，用于解释 A 股或数字货币的价格结构状态。

## 输入
只读取 structural-lite 的 smc_lite_v2 输出，包括结构事件、BOS、CHoCH、FVG、`mitigated`/`mitigated_at`、时间周期、逐事件置信度、`confirmed_at`、限制说明和 evidence_id。

## 解读口径
先说明结构方向是否明确；再解释 BOS/CHoCH 是否提示趋势延续或性质转变；FVG 只解释为价格缺口和风险区域。第一版不得把订单块或流动性池当成强结论，因为 structural-lite 未输出成熟订单块识别。

## 禁令
不得自己标注订单块、不得自己判断 BOS/CHoCH、不得把 FVG 解释成必然回补、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
