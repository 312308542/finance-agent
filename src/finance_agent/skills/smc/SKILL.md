---
name: smc
category: technical
markets: [ashare, crypto]
requires_engine: structural_lite_smc
roundtable_role: technical_analyst
---
## 适用场景
本项目 structural-lite / smc_lite_v2 引擎已经输出 BOS、CHoCH、FVG、回补状态和置信度时，用于解释 A 股或数字货币的价格结构状态。

## 输入
只读取 structural-lite 的 smc_lite_v2 输出，包括结构事件、BOS、CHoCH、FVG、`mitigated`/`mitigated_at`、时间周期、逐事件置信度、`confirmed_at`、限制说明和 evidence_id。

## 解读口径
先说明结构方向是否明确；再解释 BOS/CHoCH 是否提示趋势延续或性质转变；FVG 只解释为价格缺口和风险区域。第一版不得把订单块或流动性池当成强结论，因为 structural-lite 未输出成熟订单块识别。

解读 BOS 时，把看涨 BOS 解释为价格突破已确认 swing 高点后的趋势延续候选，把看跌 BOS 解释为跌破已确认 swing 低点后的趋势延续候选；BOS 的价值在于确认原方向仍有效，而不是单独给出动作。

解读 CHoCH 时，把看涨 CHoCH 解释为下跌结构中突破前 swing 高点后的性质转变候选，把看跌 CHoCH 解释为上涨结构中跌破前 swing 低点后的性质转变候选；CHoCH 的反转含义强于 BOS，但在本项目中仍只作为技术结构提示，T14 风险反驳接入需等待事件研究。

解读 FVG 时，只说明公允价值缺口代表三根 K 线结构里的价格不平衡区域。看涨 FVG 可提示买方力量快速推进，看跌 FVG 可提示卖方力量快速推进；`mitigated`/`mitigated_at` 表示缺口是否已被回补，已回补或过窄缺口的解释力应降低。

订单块知识只作为背景：传统 SMC 会把快速离开前的供需区域视为潜在订单块，但 structural-lite 当前未输出成熟订单块或流动性池字段。因此模型只能说明“引擎未提供订单块证据，不得强结论”，不能自行标注订单块。

## 失效条件
看涨 BOS/CHoCH 的失效条件是价格重新跌回被突破的已确认 swing 高点下方，或后续结构事件转为反向 CHoCH；看跌 BOS/CHoCH 的失效条件是价格重新站回被跌破的已确认 swing 低点上方，或后续结构事件转为反向 CHoCH。

FVG 的失效或降级条件包括：缺口已被回补、缺口宽度低于 ATR 地板、缺口方向与当前结构事件相反，或引擎标记为 `insufficient_structure`。FVG 不等于必然回补，只能解释为风险区域或不平衡区域。

若 payload 只有少量孤立事件、置信度低、确认时间过旧，或结构事件与系统信号/风险反驳冲突，应提示结构证据弱，不得把 BOS、CHoCH 或 FVG 升级为确定结论。

## 禁令
不得自己标注订单块、不得自己判断 BOS/CHoCH、不得把 FVG 解释成必然回补、不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。

## 来源
来源：Vibe-Trading references，MIT，已按只读解读视角改写。
