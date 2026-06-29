---
name: chanlun-interpret
category: technical
markets: [ashare, crypto]
requires_engine: chanlun
roundtable_role: technical_analyst
---
## 适用场景
当 `chanlun` 确定性引擎已经给出分型、笔、中枢和一买/二买/三买/一卖/二卖/三卖等结构时，用于技术分析师解释结构含义。

## 输入
只引用 `ChanlunAdapter` 或未来 `czsc` 适配器输出的 `patterns`、`signals`、`evidence_id`、K 线周期、引擎版本和已入库的行情/风险上下文。

## 解读口径
先说明当前结构属于趋势延续、背驰风险还是中枢震荡；再解释买卖点信号对应的条件；最后指出和量价、波动、风险反驳之间的冲突。

## 禁令
不得自己数笔、不得自己画中枢、不得自行判定买卖点；只引用 chanlun 引擎输出；不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。
