# AKShare A 股数据能力矩阵

本文档把 AKShare 官方股票数据能力映射到 Hermes AI 标的推荐系统。目标不是“把接口全部堆进来”，而是让每类数据都服务于完整推荐链路：

```text
候选池来源/种子池
 -> AssetUniverse 构建和基础过滤
 -> 数据采集
 -> 因子计算
 -> 初筛规则
 -> 多维评分
 -> Agent 分析
 -> 风险反驳
 -> 推荐榜/观察池/回避池
 -> 中文解释报告
```

AKShare 在本系统里应视为 **A 股数据 Provider 族**，不是单一 K 线接口。LLM/Agent 不直接调用 AKShare，只消费已经归一化、落库和计算后的结构化数据。

## 0. 术语边界

本文里的“候选池”是推荐链路的输入范围，不是 Agent 分析后的推荐结论。

- **候选池来源/种子池**：全 A、指数成分、行业/概念成分、资金流榜单、热度榜等由数据源直接给出的初始范围。
- **AssetUniverse 候选池**：系统把一个或多个种子源合并、去重、基础过滤后形成的本次推荐输入。
- **待分析名单**：经过因子、信号、初筛和多维评分后，送给 Agent 解释和反驳的较小标的集合。
- **推荐结果**：Agent 和排序服务最终输出的推荐榜、观察池、回避池和中文报告。

所以“资金流池”“热度池”不是 Agent 推荐之后的池子，而是用资金流排名、热度榜、涨停池等数据构造出来的上游种子源。它们只能说明“值得纳入筛选”，不能直接等同于“可以买”。

## 1. 接入原则

- AKShare 只在 `finance_agent/data/providers/` 适配层调用。
- Provider 输出统一模型，不把 AKShare 原始 DataFrame 传给 application、agents 或 API。
- AKShare 接口必须先登记到 `finance_agent/data/akshare_capabilities.py`，再进入 Provider、任务调度或因子服务。
- 所有原始响应进入 `raw_records`，用于追溯和排错。
- 高频查询字段单独结构化，扩展字段放入 `payload`。
- 不可靠接口必须有 `status`、`error_message` 和 fallback。
- 东方财富接口优先使用 AKShare；若本机触发断连，可用 `curl_cffi` 或腾讯接口兜底。
- 腾讯接口可作为 A 股行情 fallback，尤其是实时行情和日线行情。
- 腾讯实时行情 fallback 受分页和网络稳定性影响，第一版用于降级保底；完整全 A 主源仍优先使用东方财富或后续专门的股票列表接口。

工程上采用“注册表尽量全、执行分批接”的方式：

- `akshare_capabilities.py` 尽可能登记 AKShare 股票数据对推荐系统有价值的接口，记录 Provider、数据域、推荐链路位置、落库表、因子组、优先级和 fallback。
- P0/P1 接口优先做真实调用、落库和健康检查，保证 A 股推荐主链路可用。
- P2/P3 接口先进入注册表和健康检查，再按因子、风险、事件和报告质量逐批接入。
- 不按 AKShare 模块机械建表，仍以 `fundamental_snapshots`、`capital_flow_snapshots`、`event_records`、`risk_findings`、`evidence` 等通用表承载。

## 2. Provider 拆分

| Provider | 职责 | 主要输出 | 推荐链路位置 |
| --- | --- | --- | --- |
| `AshareUniverseProvider` | 全 A、指数成分、行业/概念成分、资金流/热度榜单种子 | `AssetData`、候选池来源、`AssetUniverse` 成员 | 候选池构建 |
| `AshareMarketDataProvider` | 实时行情、日线/周线/月线、成交量、成交额 | `MarketBarData`、实时快照 | 数据采集、技术因子 |
| `AshareFundamentalProvider` | 财报、主要指标、业绩报表、业绩快报、业绩预告 | 基本面快照 | 基本面因子 |
| `AshareValuationProvider` | PE/PB/股息率/市场估值/个股估值 | 估值快照 | 估值因子、风险反驳 |
| `AshareCapitalFlowProvider` | 主力资金、行业/概念资金流、北向资金 | 资金流快照、资金流榜单种子 | 资金流因子、候选池来源 |
| `AshareSectorProvider` | 行业板块、概念板块、板块成分、板块行情 | 板块快照、成分关系 | 候选池来源、行业/主题因子 |
| `AshareEventProvider` | 公告、新闻、财报披露、分红、停复牌、新股 | 事件记录、证据 | 事件因子、风险反驳 |
| `AshareRiskProvider` | 停牌、退市、质押、限售解禁、龙虎榜、融资融券、大宗交易 | 风险发现、证据 | 初筛、风险反驳 |
| `AshareSentimentProvider` | 热度、人气榜、投票、涨停池、盘口异动 | 情绪/热度快照、热度榜单种子 | 情绪因子、候选池来源、事件解释 |
| `AshareMarketBreadthProvider` | 市场总貌、估值水位、股债利差、拥挤度、赚钱效应 | 市场环境快照 | 大盘过滤、仓位建议 |

## 3. AKShare 模块映射

| AKShare 能力 | 代表接口或模块 | 系统用途 | 优先级 |
| --- | --- | --- | --- |
| 实时行情 | `stock_zh_a_spot_em`、`stock_zh_a_spot_tx` | 资产主数据、全 A 种子池、实时价格、成交额、PE/PB、市值 | P0 |
| 历史行情 | `stock_zh_a_hist`、`stock_zh_a_hist_tx` | K 线、技术指标、回测价格序列 | P0 |
| 股票列表 | `股票列表-A股/上证/深证/北证` 相关接口 | 资产主数据、交易所归属、可交易状态 | P0 |
| 停复牌/退市 | `stock_zh_a_stop_em`、两网及退市、暂停/终止上市 | 初筛硬过滤、风险提示 | P0 |
| 行业板块 | `stock_board_industry_*` | 行业种子池、行业动量、行业轮动 | P1 |
| 概念板块 | `stock_board_concept_*` | 主题种子池、主题热度、事件解释 | P1 |
| 资金流向 | `stock_individual_fund_flow`、`stock_main_fund_flow`、`stock_sector_fund_flow_*` | 主力资金、板块资金、资金流因子、资金流榜单种子 | P1 |
| 沪深港通资金流 | `stock_hsgt_*` | 北向资金、外资偏好、资金确认 | P1 |
| 个股新闻 | `stock_news_*`、个股新闻 | 事件证据、Agent 中文解释 | P1 |
| 公告/披露 | `stock_notice_report`、`stock_individual_notice_report`、`stock_zh_a_disclosure_report_cninfo` | 重大事件、财报披露、风险反驳 | P1 |
| 财报发行 | 财报发行、预约披露时间 | 数据时效、财报事件提醒 | P1 |
| 年报季报 | `stock_yjbb_em`、`stock_yjkb_em`、`stock_yjyg_em` | 成长、盈利质量、业绩变化 | P2 |
| 财务报表 | 资产负债表、利润表、现金流量表相关接口 | ROE、现金流、负债率、利润增长 | P2 |
| 主要财务指标 | `stock_financial_analysis_indicator_em`、同花顺/新浪指标 | 基本面分组核心输入 | P2 |
| 盈利预测 | `stock_profit_forecast_em`、`stock_profit_forecast_ths` | 预期差、机构一致预期 | P2 |
| 股东/高管 | 十大股东、流通股东、高管持股、股东户数 | 筹码稳定性、治理风险 | P2 |
| 股权质押 | 股票质押、上市公司质押比例、质押明细 | 质押风险、风险扣分 | P2 |
| 分红配送 | 分红派息、历史分红、股息率 | 价值/红利因子 | P2 |
| 新股数据 | 新股申购、中签、上市首日、次新股 | 新股种子池、次新风险过滤 | P2 |
| 融资融券 | `stock_margin_*`、两融账户、标的证券名单 | 杠杆情绪、拥挤风险 | P2 |
| 大宗交易 | `stock_dzjy_*` | 机构交易、折溢价异常 | P2 |
| 龙虎榜 | `stock_lhb_*` | 游资/机构活跃、短线情绪、风险提示 | P2 |
| 热度/人气 | `stock_hot_rank_*`、热搜、投票 | 情绪因子、用户解释 | P2 |
| 盘口/板块异动 | 盘口异动、板块异动详情 | 盘中异动解释，不作为强推荐依据 | P2 |
| 涨停板行情 | `stock_zt_pool_*` | 短线情绪、强势池、风险提示 | P2 |
| 赚钱效应 | 赚钱效应分析 | 市场环境、仓位建议 | P2 |
| 技术指标榜单 | 创新高、创新低、连续上涨、突破、量价齐升等 | 技术榜单种子、技术面标签 | P2 |
| 估值指标 | A 股估值、个股估值、股息率、股债利差、巴菲特指标 | 市场水位、估值风险 | P2 |
| 机构调研/研报 | 机构调研、个股研报、机构推荐 | 事件证据、基本面解释 | P3 |
| ESG 评级 | ESG 评级数据、MSCI、路孚特、华证 | 长周期风险和偏好标签 | P3 |

## 4. 推荐链路中的使用方式

### 4.1 候选池来源和种子池

候选池不只来自全 A。这里的“候选池”指 `AssetUniverse`，是推荐链路的输入，不是推荐结论。一次推荐运行可以指定一个种子源，也可以把多个种子源合并、去重后形成候选池。

- 全 A 种子源：实时行情或股票列表。
- 指数成分种子源：沪深 300、中证 500、创业板等指数成分。
- 行业成分种子源：行业板块成分。
- 概念成分种子源：概念板块成分。
- 资金流榜单种子源：主力资金、北向资金、行业资金排名，只说明近期资金有异动，不能直接视为推荐。
- 情绪/热度种子源：热度榜、涨停池、盘口异动，只用于纳入观察和解释短线情绪，不能直接视为推荐。
- 风险排除源：停牌、退市整理、ST、流动性不足、数据缺失，用于从候选池里剔除或降级。

Agent 分析之后输出的是推荐榜、观察池和回避池。为避免混淆，后续代码和文档里应尽量使用 `universe_source`、`seed_source`、`AssetUniverse`、`watchlist`、`avoid_list` 这些更明确的命名。

### 4.2 数据采集

第一版推荐至少采集：

- 日线 K 线。
- 实时行情快照。
- 成交额、换手率、市值、PE/PB。
- 停复牌和退市状态。
- 主力资金和北向资金。
- 行业/概念归属。
- 最新公告、新闻、财报披露状态。

### 4.3 因子计算

| 因子组 | AKShare 输入 | 示例因子 |
| --- | --- | --- |
| 技术面 | 日线、成交量、技术榜单 | 动量、趋势、波动、放量、突破 |
| 基本面 | 财报、主要指标、业绩报表 | ROE、营收增长、利润增长、现金流质量、负债率 |
| 估值 | PE/PB、股息率、市场估值 | 估值分位、红利因子、估值过热风险 |
| 资金流 | 主力资金、北向资金、板块资金 | 净流入强度、连续流入、外资确认、资金背离 |
| 事件 | 公告、新闻、研报、财报披露 | 重大利好/利空、业绩预告变化、事件新鲜度 |
| 情绪 | 热度榜、涨停池、盘口异动 | 市场关注度、短线拥挤度、情绪过热 |
| 风险 | 停复牌、质押、限售、两融、龙虎榜 | 交易状态风险、质押风险、杠杆拥挤、异常交易 |

### 4.4 初筛规则

硬过滤优先使用确定性数据：

- 停牌、退市整理、终止上市。
- 上市时间过短且非“次新策略”。
- 成交额或换手率过低。
- 近 N 日 K 线缺失。
- 高质押比例或重大风险公告。
- 财报严重缺失或披露延迟。
- 盘中异动但无足够日线确认的标的，只进入观察池。

### 4.5 Agent 使用

Agent 不重新抓 AKShare，不重新算分。Agent 只消费：

- `factor_frames`：因子分组和缺失情况。
- `signal_snapshots`：方向、置信度、触发原因。
- `risk_findings`：反方观点和风险扣分。
- `evidence`：新闻、公告、财报、资金流、行情证据。

## 5. 存储落点

| 数据类型 | 当前表或建议表 | 说明 |
| --- | --- | --- |
| 资产主数据 | `assets` | A 股代码、名称、交易所、行业、状态 |
| 候选池 | `asset_universes`、`asset_universe_members` | 由全 A、指数、行业/概念、资金流/热度等种子源生成的推荐输入范围，不是推荐结果 |
| 原始响应 | `raw_records` | 所有 AKShare 原始响应归档 |
| K 线 | `market_bars` | TimescaleDB hypertable |
| 财务估值 | `fundamental_snapshots` | M1 表，承载财务、估值、业绩 |
| 资金流 | `capital_flow_snapshots` | M1 表，承载主力、北向、板块资金 |
| 事件公告 | `event_records` | M1 表，承载新闻、公告、披露、分红、停复牌事件 |
| 指标快照 | `indicator_frames` | 推荐时点关键技术指标 |
| 因子快照 | `factor_frames` | 推荐时点因子分组结果 |
| 风险发现 | `risk_findings` | 停牌、质押、两融、龙虎榜、事件风险 |
| 证据索引 | `evidence` | 报告中引用的数据和文本证据 |

不建议为 AKShare 每个模块单独建一张表。第一版优先用通用快照表和 `payload` 承载差异，等某类数据成为高频查询或排序字段后，再拆专表。

## 6. 实现优先级

### P0：让 A 股主链路可跑

- `stock_zh_a_spot_em`，失败时 fallback 到 `stock_zh_a_spot_tx`。
- `stock_zh_a_hist`，失败时 fallback 到 `stock_zh_a_hist_tx`。
- 停牌/退市状态。
- 写入 `assets`、`market_bars`、`AssetUniverse` 候选池输入范围。

### P1：让选股有基本解释

- 行业/概念板块和成分。
- 主力资金、行业资金、北向资金。
- 个股新闻、公告、财报披露。
- 写入 `capital_flow_snapshots`、`event_records`、`evidence`。

### P2：让评分更像金融分析

- 财报、业绩报表、业绩快报、业绩预告。
- 主要财务指标、资产负债表、利润表、现金流量表。
- PE/PB、股息率、A 股估值水位。
- 股东变化、股权质押、限售解禁。
- 融资融券、龙虎榜、大宗交易。
- 热度榜、涨停池、盘口异动、赚钱效应。

### P3：增强报告质量和长期风险

- 盈利预测、研报、机构调研。
- ESG 评级。
- 一致行动人、机构持股、基金持股。
- 大盘拥挤度、股债利差、巴菲特指标。

## 7. 当前工程差距

当前代码已经有：

- `AkshareProvider.fetch_assets`
- `AkshareProvider.fetch_ohlcv`
- `finance_agent/data/akshare_capabilities.py`
- `scripts/data/check_akshare_capabilities.py`
- `AssetRepository`
- `UniverseRepository`
- `MarketDataRepository`
- `market_bars` TimescaleDB 存储

下一步应补：

1. 将 P1 Provider 族拆出来，不继续把所有能力塞进单个 `AkshareProvider`。
2. 为注册表中的 P0/P1 接口建立真实采集任务和 Raw Store 归档。
3. 补 `fundamental_snapshots`、`capital_flow_snapshots`、`event_records` 三类表和仓储。
4. 将 P2 财务、估值、风险、情绪接口逐批转成因子和证据。
5. 定期运行数据健康检查脚本，输出哪些 AKShare 接口可用、哪些降级。
