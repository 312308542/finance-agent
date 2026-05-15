# AKShare 能力与因子重合审计

更新时间：2026-05-15

本文档用于回答一个核心问题：AKShare 已经提供了很多 A 股数据和部分技术筛选能力，系统在做因子计算前，应先判断哪些能力可以直接复用，哪些只能作为辅助标签，哪些必须由系统自己计算。

结论先行：

- AKShare 可以承担 A 股基础数据、A 股资金/财务/估值/风险/情绪数据，以及部分同花顺技术选股榜单。
- AKShare 不能替代本系统的统一因子计算层，因为系统需要同时覆盖 A 股和数字货币，并且需要统一口径、可追溯快照、缺失标记和跨市场评分。
- 第一版因子计算直接使用 `TA-Lib` 作为技术指标主引擎，配合 `pandas/numpy` 做数据整理、窗口聚合、收益率、分位数、z-score 以及资金流/估值/衍生品派生计算。
- 当前依赖已经把 `TA-Lib` 和 `numpy` 纳入默认依赖，本地虚拟环境已验证 `talib 0.6.8` 可 import，并可计算 RSI、ATR 等基础指标。

## 1. 重合边界

| 类型 | 是否使用 AKShare | 系统处理方式 | 原因 |
| --- | --- | --- | --- |
| 原始行情、资金、财务、估值、风险、情绪 | 直接使用 | Provider 归一化后写入标准表 | AKShare 是 A 股数据源，必须保留 raw 归档和 fallback |
| 同花顺技术选股榜单 | 使用，但只作辅助标签 | 写入候选池、事件或标签，不作为最终技术因子本体 | 榜单口径外部不可控，且只覆盖 A 股 |
| Yang-Zhang 已实现波动率 | 可使用，但只作补充 | 可作为 A 股波动率参考；统一波动率仍建议系统按 OHLCV 自算 | AKShare 指标计算能力较窄，不覆盖完整技术指标体系和数字货币 |
| RSI、MACD、均线、ATR、布林带、动量、回撤 | 不依赖 AKShare | 系统使用 TA-Lib 计算技术指标，pandas/numpy 负责序列整理和派生计算 | 需要跨 A 股和数字货币统一口径 |
| 估值分位、财务质量、成长评分 | 部分使用 AKShare 输入 | 系统按历史窗口、行业分组或市场分位计算 | AKShare 给原始指标或估值序列，评分口径应由系统控制 |
| 多维评分和推荐排序 | 不使用 AKShare | 系统根据因子、风险、缺失情况和策略权重计算 | 推荐排序是系统核心决策层，不能交给数据源 |

## 2. 可直接复用的 AKShare 能力

这些能力属于“数据源即事实输入”，可以直接进入基础数据层。

| 因子组 | AKShare 输入 | 当前状态 | 推荐落点 |
| --- | --- | --- | --- |
| 资产与流动性 | `stock_zh_a_spot_em`、`stock_zh_a_spot_tx` | 已接入 P0 | `assets`、`asset_universes`、`raw_records` |
| K 线 | `stock_zh_a_hist`、`stock_zh_a_hist_tx` | 已接入 P0 | `market_bars` |
| 行业/主题 | `stock_board_industry_cons_em`、`stock_board_concept_cons_em` | 已接入 P1，带 fallback | `asset_universes`、`asset_universe_members` |
| 个股资金流 | `stock_individual_fund_flow_rank`、`stock_individual_fund_flow` | 排名已接入，个股明细待增强 | `capital_flow_snapshots` |
| 板块资金流 | `stock_main_fund_flow`、`stock_fund_flow_industry`、`stock_fund_flow_concept` | 已登记，待采集 | `capital_flow_snapshots`、市场环境快照 |
| 北向资金 | `stock_hsgt_hist_em`、`stock_hsgt_individual_em` | 已登记，待采集 | `capital_flow_snapshots` |
| 新闻公告 | `stock_news_em`、`stock_notice_report` | 新闻已接入，公告待调度 | `event_records`、`evidence` |
| 财务指标 | `stock_financial_analysis_indicator_em` | 已接入 P2 | `fundamental_snapshots` |
| 业绩数据 | `stock_yjbb_em`、`stock_yjkb_em`、`stock_yjyg_em` | 业绩报表已接入，快报/预告已登记 | `fundamental_snapshots`、`event_records` |
| 估值和股息 | `stock_value_em`、`stock_a_gxl_lg` | 估值已接入，股息率待稳定 | `fundamental_snapshots` |
| 短线情绪 | `stock_hot_rank_em`、`stock_zt_pool_em` | 已接入 P2 | `asset_universes`、`event_records`、`risk_findings` |
| 异常交易 | `stock_lhb_detail_em`、`stock_dzjy_mrmx` | 已接入 P2 | `risk_findings`、`evidence` |
| 杠杆风险 | `stock_margin_sse`、`stock_margin_szse` | 已接入 P2 | `risk_findings` |

## 3. 只作为辅助标签的 AKShare 能力

AKShare 官方股票文档包含同花顺技术选股榜单，例如创新高、创新低、连续上涨、持续放量、向上突破、量价齐升等。这类接口适合做候选池增强和解释标签，但不应替代系统技术因子。

| AKShare 接口 | 官方含义 | 系统建议用途 | 是否进入最终因子 |
| --- | --- | --- | --- |
| `stock_rank_cxg_ths` | 创新高 | 动量候选标签、突破证据 | 可作为 `technical_tags`，不直接打分 |
| `stock_rank_cxd_ths` | 创新低 | 风险/弱势标签、反向观察池 | 可作为风险标签 |
| `stock_rank_lxsz_ths` | 连续上涨 | 强势候选标签 | 可辅助动量因子 |
| `stock_rank_lxxd_ths` | 连续下跌 | 弱势/回避标签 | 可辅助风险因子 |
| `stock_rank_cxfl_ths` | 持续放量 | 放量确认标签 | 可辅助成交量因子 |
| `stock_rank_cxsl_ths` | 持续缩量 | 流动性下降标签 | 可辅助流动性风险 |
| `stock_rank_xstp_ths` | 向上突破均线 | 突破标签 | 系统仍需用 K 线复算突破 |
| `stock_rank_xxtp_ths` | 向下突破均线 | 破位标签 | 系统仍需用 K 线复算破位 |
| `stock_rank_ljqs_ths` | 量价齐升 | 短线强势标签 | 可辅助情绪/技术解释 |
| `stock_rank_ljqd_ths` | 量价齐跌 | 弱势和风险标签 | 可辅助风险解释 |

第一版建议：这些接口先进入 `event_records` 或 `asset_universes` 的种子来源，不直接写入 `indicator_frames`。进入评分前必须由系统用 `market_bars` 复算核心技术指标。

## 4. 必须由系统计算的因子

这些因子需要跨 A 股和数字货币统一，不能依赖 AKShare A 股专用接口。

| 因子组 | 系统计算因子 | 输入表 | 需要组件 |
| --- | --- | --- | --- |
| 技术面 | `return_1d`、`return_5d`、`return_20d`、`momentum_20d`、`ma_20`、`ma_60`、`ma_slope`、`rsi_14`、`macd`、`atr_14`、`bb_percent_b` | `market_bars` | `TA-Lib` 计算核心技术指标；`pandas/numpy` 计算收益率、窗口聚合和派生指标 |
| 波动与风险 | `volatility_20d`、`realized_volatility`、`max_drawdown_20d`、`downside_volatility` | `market_bars` | `pandas/numpy` 计算窗口统计；ATR 等技术波动指标走 `TA-Lib`；YZ 波动率可参考 AKShare |
| 流动性 | `amount_avg_20d`、`turnover_avg_20d`、`volume_zscore`、`illiquidity_score` | `market_bars`、实时行情 | `pandas/numpy` |
| 资金流 | `main_net_inflow_strength`、`flow_rank_percentile`、`flow_continuity`、`flow_price_divergence` | `capital_flow_snapshots`、`market_bars` | `pandas/numpy` |
| 基本面 | `roe_score`、`revenue_growth_score`、`profit_growth_score`、`cashflow_quality`、`debt_risk_score` | `fundamental_snapshots` | `pandas/numpy` |
| 估值 | `pe_percentile`、`pb_percentile`、`dividend_score`、`valuation_overheat` | `fundamental_snapshots`、历史估值 | `pandas/numpy` |
| 情绪 | `hot_rank_score`、`limit_up_crowding`、`attention_change` | `event_records`、`asset_universes`、`risk_findings` | `pandas/numpy` |
| 事件 | `event_freshness`、`negative_event_count`、`announcement_risk` | `event_records`、`evidence` | 规则引擎，后续可接 NLP |
| A 股风险 | `trading_status_block`、`margin_crowding`、`lhb_risk`、`block_trade_discount_risk` | `risk_findings` | 规则引擎 |
| 数字货币衍生品 | `funding_rate_zscore`、`open_interest_change`、`long_short_crowding`、`basis_rate` | `crypto_derivative_snapshots`、`market_bars` | `pandas/numpy` |

## 5. 分析参数建议

第一版不要把所有参数开放成无限组合。推荐先固定一组可解释参数，后续再做策略配置。

| 场景 | 参数 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 日线波段 | `timeframe` | `1d` | A 股和数字货币都可用 |
| 波段周期 | `horizon` | `swing` | 第一版推荐主场景 |
| 技术输入窗口 | `bar_window` | `120` | 足够计算 MA60、MACD、ATR 和回撤 |
| 短动量 | `return_windows` | `5,20,60` | 兼顾短线和中期趋势 |
| 均线 | `ma_windows` | `20,60` | 第一版先不扩展到过多均线 |
| RSI | `rsi_period` | `14` | 通用默认值 |
| MACD | `macd_periods` | `12,26,9` | 通用默认值 |
| ATR | `atr_period` | `14` | 风险和波动输入 |
| 布林带 | `bb_period` | `20` | 趋势和波动位置 |
| 估值分位窗口 | `valuation_window_days` | `756` | 约三年交易日 |
| 资金连续性窗口 | `flow_windows` | `1,3,5,10,20` | 与 AKShare 资金流榜单口径对齐 |
| 事件新鲜度 | `event_decay_days` | `30` | 新事件权重大，旧事件衰减 |
| 数字货币衍生品窗口 | `derivatives_window` | `24h,7d,30d` | 覆盖日内拥挤和中期风险 |

## 6. 当前依赖评估

当前 `pyproject.toml` 依赖：

- 已有：`akshare`、`ccxt`、`curl_cffi`、`numpy`、`pandas`、`TA-Lib`、`psycopg`、`redis`、`requests`、`sqlalchemy`、`alembic`。
- 运行环境实测可 import：`pandas`、`numpy`、`akshare`、`ccxt`、`talib`。
- 运行环境实测不可 import：`ta`、`pandas_ta`、`scipy`、`sklearn`；这些不进入第一版默认主链路。

评估结论：

| 能力 | 当前依赖是否满足 | 说明 |
| --- | --- | --- |
| 基础数据采集 | 满足 | AKShare、ccxt、curl_cffi、pandas 已足够 |
| 归一化和落库 | 满足 | SQLAlchemy、psycopg、TimescaleDB 已接入 |
| 基础技术因子 | 满足 | `numpy` 已显式依赖；收益率、均线、波动率、回撤由 pandas/numpy 计算 |
| 完整技术指标 | 满足 | 主路径固定为 `TA-Lib/talib`，本地已验证 `talib 0.6.8` 可用 |
| A 股资金/财务/估值因子 | 部分满足 | 数据输入已有，`FactorService` 基础版已能合并最新快照；历史分位、行业分组和完整评分口径待补 |
| 数字货币衍生品因子 | 部分满足 | 数据输入已有，`FactorService` 基础版已能合并最新快照；z-score、变化率和批量窗口计算待补 |
| 机器学习因子 | 不满足，且第一版不建议 | 缺 scipy/sklearn；第一版不需要 ML 主链路 |

依赖建议：

1. 第一版因子服务显式依赖 `numpy`，不要只依赖 pandas 的传递依赖。
2. 第一版技术指标主路径固定为 `TA-Lib`，代码导入名为 `talib`，进入默认依赖。
3. `pandas/numpy` 配合 `TA-Lib` 使用：负责数据清洗、窗口聚合、收益率、分位数、z-score、资金流、估值、基本面和数字货币衍生品派生因子。
4. `ta` 暂不作为默认依赖，仅作为未来备用方案评估；当前不引入 `pandas_ta`、`scipy`、`sklearn`、`vectorbt`，避免第一版复杂度扩散。

## 7. 实施顺序建议

1. 已实现 `IndicatorService` 基础版：读取 `market_bars`，用 TA-Lib + pandas/numpy 计算基础技术指标，写入 `indicator_frames`。
2. 已实现 `FactorService` 基础版：合并技术指标、资金流、基本面、估值、事件、风险和数字货币衍生品快照，写入 `factor_frames`，并记录缺失因子组和部分可用因子组。
3. 下一步补 `factor_spec` 或等价配置，把第一版系统计算因子、输入表、窗口参数、缺失策略和输出字段固化下来。
4. 把 AKShare 技术选股榜单作为 `technical_tags` 或候选池种子，不直接替代 `indicator_frames`。
5. 再实现初筛、多维评分和推荐排序。
