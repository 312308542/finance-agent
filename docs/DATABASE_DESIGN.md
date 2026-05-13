# 数据库设计

本文档定义 Hermes AI 标的推荐系统的数据库表设计。第一版同时覆盖 A 股和数字货币，主链路是：

```text
候选池
 -> 数据采集
 -> 因子计算
 -> 初筛规则
 -> 多维评分
 -> Agent 分析
 -> 风险反驳
 -> 推荐排序
 -> 中文解释报告
```

数据库目标：

- 支撑 A 股和数字货币共用一套推荐链路。
- 全环境统一使用 PostgreSQL + TimescaleDB：业务实体用普通 PostgreSQL 表，行情和衍生品时间序列用 TimescaleDB hypertable。
- 保留原始数据，方便追溯和审计。
- 结构化字段用于查询、筛选、排序和索引。
- 复杂协议全文保存到 `payload JSON`，方便协议升级。
- 不把 Agent 的自然语言输出当作唯一事实来源，所有推荐必须能追到因子、信号、风险和证据。

## 1. 设计原则

### 1.1 存储选型

统一方案：

- PostgreSQL：保存资产、候选池、因子、评分、推荐、证据、回测等业务数据。
- TimescaleDB：保存 K 线、资金费率、未平仓量、多空比等时间序列数据。

环境要求：

- 开发、测试、演示和生产都使用 PostgreSQL + TimescaleDB，保持 schema、约束、hypertable 行为一致。
- 不为主链路提供 SQLite 或普通 PostgreSQL 降级模式，避免开发环境绕过 TimescaleDB 的唯一约束、时间分区和压缩策略。
- SQLite 只能用于完全脱离推荐主链路的一次性脚本实验，不能作为应用运行环境或测试环境。

需要使用 TimescaleDB hypertable 的表：

- `market_bars`
- `crypto_derivative_snapshots`

后续数据量变大时，也可以评估把这些表迁移或同步到 ClickHouse / Parquet 做离线研究，但系统主库仍以 PostgreSQL + TimescaleDB 为准。

### 1.2 共用表优先

A 股和数字货币都属于 `assets`。市场差异通过 `market`、`exchange`、`asset_type`、`payload` 和专属快照表表达。

共用表：

- `assets`
- `asset_universes`
- `asset_universe_members`
- `raw_records`
- `market_bars`
- `market_calendars`
- `indicator_frames`
- `factor_frames`
- `screening_results`
- `screening_result_items`
- `asset_scores`
- `signal_snapshots`
- `risk_findings`
- `recommendation_runs`
- `recommendation_run_universes`
- `asset_recommendations`
- `agent_analysis_runs`
- `agent_analysis_items`
- `evidence`

专属表：

- A 股：`fundamental_snapshots`、`capital_flow_snapshots`
- 数字货币：`crypto_derivative_snapshots`

### 1.3 字段和 payload 的边界

需要高频查询、筛选、排序的字段单独建列。

例如：

- `asset_id`
- `symbol`
- `market`
- `timeframe`
- `as_of`
- `status`
- `total_score`
- `rank`
- `action`

变化快、字段多、不同市场差异大的内容放入 `payload JSON`。

例如：

- AKShare 原始响应。
- Binance 原始响应。
- 因子分组明细。
- Agent 分析摘要。
- 推荐理由和风险反驳。
- 回测参数和图表数据。

### 1.4 时间字段

统一使用带时区时间。

- `as_of`：数据本身对应的市场时间。
- `collected_at`：系统采集时间。
- `created_at`：系统生成记录时间。
- `updated_at`：系统最后更新时间。

### 1.5 状态字段

所有核心数据表都应有 `status`。

```text
available    完整可用
partial      部分可用
unavailable  不可用
stale        过期
error        获取或计算失败
```

### 1.6 ID 规范

建议 ID 使用可读字符串，方便调试和跨表追溯。

```text
asset_id:             ashare:600519 / crypto_spot:BTCUSDT
universe_id:          universe:hs300:20260511
raw_record_id:        raw:akshare:stock_zh_a_hist:600519:20260511
indicator_frame_id:  ind:BTCUSDT:1d:20260511
factor_frame_id:      factor:600519:swing:20260511
screening_item_id:    screening_item:hs300:600519:20260511
score_id:             score:BTCUSDT:swing:20260511
recommendation_id:    asset_rec:600519:swing:20260511
agent_analysis_id:    agent:technical:BTCUSDT:20260511
evidence_id:          ev:binance:kline:BTCUSDT:20260511
```

### 1.7 TimescaleDB 分区原则

`market_bars` 和 `crypto_derivative_snapshots` 按时间列创建 hypertable：

- `market_bars` 使用 `timestamp` 作为时间列。
- `crypto_derivative_snapshots` 使用 `as_of` 作为时间列。

TimescaleDB 约束规则必须在表结构里体现：

- hypertable 的主键或唯一约束必须包含时间列。
- `market_bars` 不使用单列 `id` 作为主键，使用 `asset_id, timeframe, timestamp, source, adjustment` 作为复合主键或唯一约束。
- `crypto_derivative_snapshots` 不使用单列 `snapshot_id` 作为主键，使用 `asset_id, as_of, source` 作为复合主键或唯一约束。
- 涉及唯一约束的文本字段不能为 `NULL`，例如 `adjustment` 默认使用空字符串。

建议按市场和周期设置不同 chunk 粒度：

| 数据 | 推荐 chunk 粒度 |
| --- | --- |
| A 股日线 | 6 个月 |
| A 股分钟线 | 7 天到 1 个月 |
| 数字货币 1d / 4h / 1h | 1 个月 |
| 数字货币 1m / 5m | 1 天到 7 天 |
| 衍生品快照 | 7 天到 1 个月 |

M0 可以先统一使用 1 个月 chunk，后续根据数据量调整。

建议开启压缩策略：

- 高频 K 线：30 天后压缩。
- 日线和低频数据：180 天后压缩。
- 衍生品快照：30 天后压缩。

保留策略先不自动删除，避免影响回测和审计。后续如数据量过大，再对 1m/5m 高频数据设置归档到 Parquet。

## 2. 表总览

| 分组 | 表名 | 用途 | 优先级 |
| --- | --- | --- | --- |
| 资产与候选池 | `assets` | A 股和数字货币资产主数据 | M0 |
| 资产与候选池 | `asset_universes` | 候选池定义 | M0 |
| 资产与候选池 | `asset_universe_members` | 候选池成员 | M0 |
| 原始与行情 | `raw_records` | 原始响应归档 | M0 |
| 原始与行情 | `market_bars` | 标准 OHLCV，TimescaleDB hypertable | M0 |
| 原始与行情 | `market_calendars` | 交易日历、开收盘和休市信息 | M0 |
| A 股数据 | `fundamental_snapshots` | 财务和估值快照 | M1 |
| A 股数据 | `capital_flow_snapshots` | 资金流快照 | M1 |
| 数字货币数据 | `crypto_derivative_snapshots` | 资金费率、未平仓量、多空比，TimescaleDB hypertable | M1 |
| 事件与证据 | `event_records` | 新闻、公告、监管、链上事件 | M1 |
| 事件与证据 | `evidence` | 推荐证据索引 | M0 |
| 因子与信号 | `indicator_frames` | 技术指标计算结果和输入窗口 | M0 |
| 因子与信号 | `factor_frames` | 因子分组结果 | M0 |
| 因子与信号 | `screening_results` | 初筛结果 | M0 |
| 因子与信号 | `screening_result_items` | 单标的初筛通过/剔除明细 | M0 |
| 因子与信号 | `asset_scores` | 多维评分和排序基础 | M0 |
| 因子与信号 | `signal_snapshots` | 可解释信号快照 | M0 |
| 风险与推荐 | `risk_findings` | 风险发现和反方观点来源 | M0 |
| 风险与推荐 | `recommendation_runs` | 一次推荐运行 | M0 |
| 风险与推荐 | `recommendation_run_universes` | 一次推荐运行关联的一个或多个候选池 | M0 |
| 风险与推荐 | `asset_recommendations` | 单标的推荐结果 | M0 |
| 风险与推荐 | `agent_analysis_runs` | Agent 分析运行审计 | M0 |
| 风险与推荐 | `agent_analysis_items` | Agent 对单标的分析、反驳和解释明细 | M0 |
| 回测与绩效 | `backtest_results` | 回测结果 | M1 |
| 回测与绩效 | `performance_reports` | 绩效指标和报告 | M1 |

## 3. 资产与候选池

### 3.1 assets

资产主数据表。A 股、数字货币现货、数字货币合约都在这里登记。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `asset_id` | string | 主键，例如 `ashare:600519`、`crypto_spot:BTCUSDT` |
| `symbol` | string | 交易代码 |
| `name` | string | 展示名称 |
| `market` | string | `ashare`、`crypto_spot`、`crypto_future` |
| `asset_type` | string | `stock`、`crypto`、`cash` 等 |
| `exchange` | string | SSE、SZSE、Binance 等 |
| `currency` | string | CNY、USDT、USD |
| `sector` | string | A 股行业或数字货币分类，可为空 |
| `base_asset` | string | 数字货币基础资产，例如 BTC |
| `quote_asset` | string | 数字货币计价资产，例如 USDT |
| `tradable` | bool | 当前是否可交易 |
| `status` | string | 数据状态 |
| `payload` | JSON | 市场专属扩展字段 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

主键：

- `asset_id`

索引：

- `idx_assets_market_symbol`：`market, symbol`
- `idx_assets_exchange`：`exchange`
- `idx_assets_sector`：`sector`
- `idx_assets_status`：`status`

唯一约束：

- `market, symbol`

### 3.2 asset_universes

候选池定义表，兼容 A 股股票池和数字货币币种池。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `universe_id` | string | 主键 |
| `name` | string | 候选池名称 |
| `source` | string | `akshare:index_components`、`binance:spot_markets` 等 |
| `market` | string | 候选池所属市场 |
| `strategy_context` | string | 推荐策略场景 |
| `owner_id` | string | 所属用户或系统，可为空表示系统内置池 |
| `visibility` | string | system、private、shared |
| `base_universe_id` | string | 派生候选池的来源池，可为空 |
| `total_before_filter` | int | 过滤前数量 |
| `total_after_filter` | int | 过滤后数量 |
| `filters` | JSON | 候选池构建过滤规则 |
| `status` | string | 数据状态 |
| `as_of` | datetime | 候选池对应时间 |
| `created_at` | datetime | 创建时间 |
| `payload` | JSON | 扩展字段 |

主键：

- `universe_id`

索引：

- `idx_universes_market_as_of`：`market, as_of`
- `idx_universes_source`：`source`
- `idx_universes_strategy`：`strategy_context`
- `idx_universes_owner_visibility`：`owner_id, visibility`

### 3.3 asset_universe_members

候选池成员表。一条记录表示一个标的在一次候选池中。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 主键 |
| `universe_id` | string | 候选池 ID |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 冗余交易代码，便于查询 |
| `market` | string | 冗余市场 |
| `included` | bool | 是否进入过滤后的候选池 |
| `removed_reason` | string | 被剔除原因，可为空 |
| `rank_hint` | int | 数据源原始排名，可为空 |
| `as_of` | datetime | 对应时间 |
| `payload` | JSON | 扩展字段 |

主键：

- `id`

唯一约束：

- `universe_id, asset_id`

索引：

- `idx_universe_members_universe`：`universe_id`
- `idx_universe_members_asset`：`asset_id`
- `idx_universe_members_market_symbol`：`market, symbol`

## 4. 原始数据与标准行情

### 4.1 raw_records

原始数据归档表。所有 Provider 的原始响应都写入这里，不覆盖。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `raw_record_id` | string | 主键 |
| `provider` | string | akshare、binance、ccxt_binance 等 |
| `endpoint` | string | 接口名或方法名 |
| `asset_id` | string | 关联资产，可为空 |
| `symbol` | string | 交易代码，可为空 |
| `market` | string | 市场，可为空 |
| `request_params` | JSON | 请求参数 |
| `request_hash` | string | 请求参数归一化后的哈希，用于去重和幂等 |
| `response_payload` | JSON | 原始响应或结构化转储 |
| `content_hash` | string | 响应内容哈希，用于识别重复响应 |
| `provider_version` | string | 数据源或适配器版本 |
| `latency_ms` | int | 请求耗时 |
| `retry_count` | int | 重试次数 |
| `status` | string | 采集状态 |
| `error_message` | text | 错误信息 |
| `as_of` | datetime | 数据对应时间 |
| `collected_at` | datetime | 采集时间 |

主键：

- `raw_record_id`

索引：

- `idx_raw_records_provider_endpoint`：`provider, endpoint`
- `idx_raw_records_asset_as_of`：`asset_id, as_of`
- `idx_raw_records_collected_at`：`collected_at`
- `idx_raw_records_status`：`status`
- `idx_raw_records_request_hash`：`provider, endpoint, request_hash`
- `idx_raw_records_content_hash`：`content_hash`

### 4.2 market_bars

标准 OHLCV 行情表，A 股和数字货币共用。全环境使用 TimescaleDB hypertable。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 交易代码 |
| `market` | string | 市场 |
| `timeframe` | string | 1d、1h、4h 等 |
| `timestamp` | datetime | K 线时间 |
| `end_timestamp` | datetime | K 线结束时间，可为空 |
| `open` | decimal | 开盘价 |
| `high` | decimal | 最高价 |
| `low` | decimal | 最低价 |
| `close` | decimal | 收盘价 |
| `volume` | decimal | 成交量 |
| `amount` | decimal | 成交额，可为空 |
| `source` | string | 数据源 |
| `adjustment` | string | A 股复权方式，例如 qfq；数字货币为空字符串 |
| `is_closed` | bool | K 线是否已收盘，未收盘数据不能作为强推荐依据 |
| `raw_record_id` | string | 原始记录 ID |
| `status` | string | 数据状态 |
| `created_at` | datetime | 写入时间 |

主键或唯一约束：

- `asset_id, timeframe, timestamp, source, adjustment`

说明：

- `market_bars` 是 TimescaleDB hypertable，不使用单列 `id` 作为主键。
- `adjustment` 必须使用非空字符串，数字货币统一为空字符串，避免唯一约束中 `NULL` 导致重复写入。
- A 股推荐默认使用已收盘日线；数字货币可以使用 1h/4h/1d，但 `is_closed=false` 的最新 K 线只能作为弱证据。

索引：

- `idx_market_bars_asset_tf_time`：`asset_id, timeframe, timestamp`
- `idx_market_bars_market_symbol`：`market, symbol`
- `idx_market_bars_timestamp`：`timestamp`
- `idx_market_bars_closed`：`asset_id, timeframe, is_closed, timestamp`

TimescaleDB 设置：

```sql
SELECT create_hypertable('market_bars', 'timestamp', if_not_exists => TRUE);
```

推荐压缩设置：

```sql
ALTER TABLE market_bars SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'asset_id,timeframe',
  timescaledb.compress_orderby = 'timestamp DESC'
);
```

建议查询模式：

```sql
-- 单标的最近 N 根 K 线
SELECT *
FROM market_bars
WHERE asset_id = :asset_id
  AND timeframe = :timeframe
ORDER BY timestamp DESC
LIMIT :limit;

-- 候选池批量取时间窗口
SELECT b.*
FROM market_bars b
JOIN asset_universe_members m ON m.asset_id = b.asset_id
WHERE m.universe_id = :universe_id
  AND b.timeframe = :timeframe
  AND b.timestamp >= :start_at
  AND b.timestamp < :end_at;
```

### 4.3 market_calendars

交易日历表。A 股有交易日、节假日、停牌和开收盘时间；数字货币 24/7 交易，但仍需要记录系统采用的日切时间和统计周期边界。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `calendar_id` | string | 主键 |
| `market` | string | ashare、crypto_spot、crypto_future |
| `exchange` | string | SSE、SZSE、Binance 等 |
| `trade_date` | date | 交易日期 |
| `is_trading_day` | bool | 是否交易日 |
| `open_at` | datetime | 开盘时间，可为空 |
| `close_at` | datetime | 收盘时间，可为空 |
| `session_type` | string | regular、pre_market、continuous、closed |
| `timezone` | string | Asia/Shanghai、UTC 等 |
| `status` | string | 数据状态 |
| `source` | string | 数据源 |
| `payload` | JSON | 休市原因、节假日名称、特殊交易安排等 |

主键：

- `calendar_id`

唯一约束：

- `market, exchange, trade_date, session_type`

索引：

- `idx_market_calendars_market_date`：`market, trade_date`
- `idx_market_calendars_exchange_date`：`exchange, trade_date`

## 5. 市场专属快照

### 5.1 fundamental_snapshots

A 股财务和估值快照。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `snapshot_id` | string | 主键 |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 股票代码 |
| `report_period` | string | 报告期，例如 2026Q1 |
| `pe_ttm` | decimal | 市盈率 TTM |
| `pb` | decimal | 市净率 |
| `roe` | decimal | ROE |
| `revenue_growth_yoy` | decimal | 营收同比 |
| `net_profit_growth_yoy` | decimal | 净利润同比 |
| `debt_to_asset` | decimal | 资产负债率 |
| `operating_cashflow` | decimal | 经营现金流 |
| `source` | string | 数据源 |
| `status` | string | 数据状态 |
| `missing_fields` | JSON | 缺失字段 |
| `as_of` | datetime | 数据时间 |
| `payload` | JSON | 扩展财务字段 |

索引：

- `idx_fundamental_asset_period`：`asset_id, report_period`
- `idx_fundamental_as_of`：`as_of`
- `idx_fundamental_status`：`status`

### 5.2 capital_flow_snapshots

A 股资金流快照。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `snapshot_id` | string | 主键 |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 股票代码 |
| `main_net_inflow` | decimal | 主力净流入 |
| `northbound_net_inflow` | decimal | 北向资金净流入，可为空 |
| `turnover_rate` | decimal | 换手率 |
| `amount` | decimal | 成交额 |
| `window` | string | 1d、5d、20d 等 |
| `source` | string | 数据源 |
| `status` | string | 数据状态 |
| `as_of` | datetime | 数据时间 |
| `payload` | JSON | 扩展字段 |

索引：

- `idx_capital_flow_asset_window_asof`：`asset_id, window, as_of`
- `idx_capital_flow_symbol_asof`：`symbol, as_of`

### 5.3 crypto_derivative_snapshots

数字货币衍生品快照，用于合约风险和拥挤度分析。全环境使用 TimescaleDB hypertable。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `snapshot_id` | string | 逻辑 ID，便于外部引用 |
| `asset_id` | string | 资产 ID，例如 `crypto_future:BTCUSDT` |
| `symbol` | string | 交易对 |
| `market` | string | `crypto_spot` 或 `crypto_future` |
| `funding_rate` | decimal | 资金费率 |
| `next_funding_time` | datetime | 下次资金费率时间 |
| `open_interest` | decimal | 未平仓量 |
| `open_interest_value` | decimal | 未平仓名义价值 |
| `long_short_ratio` | decimal | 多空比 |
| `basis_rate` | decimal | 合约基差，可为空 |
| `liquidation_risk_score` | decimal | 强平风险分，可为空 |
| `source` | string | 数据源 |
| `status` | string | 数据状态 |
| `as_of` | datetime | 数据时间 |
| `payload` | JSON | 扩展字段 |

主键或唯一约束：

- `asset_id, as_of, source`

说明：

- `crypto_derivative_snapshots` 是 TimescaleDB hypertable，不使用单列 `snapshot_id` 作为主键。
- `snapshot_id` 仍保留为业务引用字段，可按 ID 在日志、证据和报告中展示。

索引：

- `idx_crypto_derivatives_asset_asof`：`asset_id, as_of`
- `idx_crypto_derivatives_symbol_asof`：`symbol, as_of`
- `idx_crypto_derivatives_status`：`status`

TimescaleDB 设置：

```sql
SELECT create_hypertable('crypto_derivative_snapshots', 'as_of', if_not_exists => TRUE);
```

推荐压缩设置：

```sql
ALTER TABLE crypto_derivative_snapshots SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'asset_id',
  timescaledb.compress_orderby = 'as_of DESC'
);
```

## 6. 事件与证据

### 6.1 event_records

新闻、公告、监管、链上和市场事件表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | string | 主键 |
| `asset_id` | string | 资产 ID，可为空 |
| `symbol` | string | 标的代码，可为空 |
| `market` | string | 市场 |
| `event_type` | string | news、announcement、regulation、onchain 等 |
| `title` | string | 标题 |
| `summary` | text | 摘要 |
| `sentiment` | string | positive、negative、neutral、unknown |
| `importance` | string | low、medium、high、critical |
| `source` | string | 来源 |
| `url` | string | 原文链接 |
| `published_at` | datetime | 发布时间 |
| `collected_at` | datetime | 采集时间 |
| `payload` | JSON | 扩展字段 |

索引：

- `idx_events_asset_published`：`asset_id, published_at`
- `idx_events_market_type`：`market, event_type`
- `idx_events_importance`：`importance`

### 6.2 evidence

证据索引表。推荐、风险、信号和报告都通过 `evidence_id` 引用证据。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `evidence_id` | string | 主键 |
| `evidence_type` | string | market_data、fundamental、indicator、news、derivatives 等 |
| `asset_id` | string | 资产 ID，可为空 |
| `source` | string | 数据源 |
| `title` | string | 证据标题 |
| `summary` | text | 证据摘要 |
| `data_ref` | string | raw_record、snapshot、bar、factor 等引用 |
| `url` | string | 外部链接，可为空 |
| `reliability` | string | low、medium、high |
| `as_of` | datetime | 证据对应时间 |
| `collected_at` | datetime | 采集时间 |
| `payload` | JSON | 扩展字段 |

索引：

- `idx_evidence_asset_asof`：`asset_id, as_of`
- `idx_evidence_type`：`evidence_type`
- `idx_evidence_source`：`source`

## 7. 因子、筛选、评分和信号

### 7.1 indicator_frames

指标结果表。保存技术指标计算结果、输入窗口、指标库版本和关键指标值，避免后续只能从 `factor_frames.payload` 里反推。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `indicator_frame_id` | string | 主键 |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 代码 |
| `market` | string | 市场 |
| `timeframe` | string | K 线周期 |
| `horizon` | string | intraday、swing、mid_term、long_term |
| `library` | string | talib、ta、custom 等 |
| `library_version` | string | 指标库版本 |
| `input_start_at` | datetime | 输入窗口开始 |
| `input_end_at` | datetime | 输入窗口结束 |
| `bar_count` | int | 输入 K 线数量 |
| `rsi_14` | decimal | RSI 14 |
| `macd` | decimal | MACD DIF 或主值 |
| `macd_signal` | decimal | MACD signal |
| `macd_hist` | decimal | MACD histogram |
| `atr_14` | decimal | ATR 14 |
| `bb_percent_b` | decimal | 布林带 %B |
| `ma_20` | decimal | 20 周期均线 |
| `ma_60` | decimal | 60 周期均线 |
| `status` | string | 数据状态 |
| `as_of` | datetime | 指标时间 |
| `payload` | JSON | 完整 IndicatorFrame 和扩展指标 |

主键：

- `indicator_frame_id`

唯一约束：

- `asset_id, timeframe, horizon, library, input_end_at`

索引：

- `idx_indicator_frames_asset_tf_asof`：`asset_id, timeframe, as_of`
- `idx_indicator_frames_market_horizon`：`market, horizon`
- `idx_indicator_frames_status`：`status`

### 7.2 factor_frames

因子结果表。保存 `FactorFrame` 协议全文和关键查询字段。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `factor_frame_id` | string | 主键 |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 代码 |
| `market` | string | 市场 |
| `horizon` | string | swing、mid_term、long_term |
| `status` | string | 数据状态 |
| `total_available_groups` | int | 可用因子组数量 |
| `missing_groups` | JSON | 缺失因子组 |
| `source_ids` | JSON | 来源 ID |
| `indicator_frame_id` | string | 技术指标结果 ID，可为空 |
| `as_of` | datetime | 数据时间 |
| `payload` | JSON | 完整 FactorFrame |

索引：

- `idx_factor_frames_asset_horizon_asof`：`asset_id, horizon, as_of`
- `idx_factor_frames_market_horizon`：`market, horizon`
- `idx_factor_frames_status`：`status`

### 7.3 screening_results

初筛结果表。一次候选池筛选对应一条记录，剔除详情放在 `payload`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `screening_id` | string | 主键 |
| `universe_id` | string | 候选池 ID |
| `strategy` | string | 推荐策略 |
| `market` | string | 市场 |
| `passed_count` | int | 通过数量 |
| `removed_count` | int | 剔除数量 |
| `rules` | JSON | 使用规则 |
| `status` | string | 状态 |
| `as_of` | datetime | 筛选时间 |
| `payload` | JSON | 完整 ScreeningResult |

索引：

- `idx_screening_universe_strategy`：`universe_id, strategy`
- `idx_screening_market_asof`：`market, as_of`

### 7.4 screening_result_items

单标的初筛明细表。用于解释每只股票或币种为什么通过、为什么被剔除，不把明细全部塞进 `screening_results.payload`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `screening_item_id` | string | 主键 |
| `screening_id` | string | 初筛 ID |
| `universe_id` | string | 候选池 ID |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 代码 |
| `market` | string | 市场 |
| `passed` | bool | 是否通过初筛 |
| `removed_reason` | string | 剔除原因，可为空 |
| `failed_rules` | JSON | 未通过的规则列表 |
| `passed_rules` | JSON | 已通过的规则列表 |
| `data_status` | string | 该标的数据可用性 |
| `liquidity_status` | string | 流动性检查结果 |
| `as_of` | datetime | 初筛时间 |
| `payload` | JSON | 单标的初筛完整上下文 |

主键：

- `screening_item_id`

唯一约束：

- `screening_id, asset_id`

索引：

- `idx_screening_items_screening_passed`：`screening_id, passed`
- `idx_screening_items_asset`：`asset_id`
- `idx_screening_items_market`：`market`

### 7.5 asset_scores

多维评分表。推荐排序主要读取这张表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `score_id` | string | 主键 |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 代码 |
| `market` | string | 市场 |
| `universe_id` | string | 候选池 ID |
| `screening_id` | string | 初筛 ID |
| `factor_frame_id` | string | 因子结果 ID |
| `horizon` | string | 周期 |
| `total_score` | decimal | 综合评分 |
| `technical_score` | decimal | 技术面分数，可为空 |
| `fundamental_score` | decimal | A 股基本面或币种项目面分数，可为空 |
| `valuation_score` | decimal | A 股估值或数字货币估值替代分数，可为空 |
| `flow_score` | decimal | A 股资金流或数字货币流动性分数，可为空 |
| `derivatives_score` | decimal | 数字货币衍生品分数，可为空 |
| `event_score` | decimal | 事件和新闻分数，可为空 |
| `risk_penalty` | decimal | 风险扣分 |
| `rank` | int | 候选池内排名 |
| `rank_in_universe` | int | 候选池总范围内排名 |
| `confidence` | decimal | 置信度 |
| `missing_penalty` | decimal | 缺失数据惩罚 |
| `rule_version` | string | 评分规则版本 |
| `status` | string | 状态 |
| `as_of` | datetime | 评分时间 |
| `payload` | JSON | 完整 AssetScore |

索引：

- `idx_asset_scores_universe_rank`：`universe_id, rank`
- `idx_asset_scores_asset_horizon_asof`：`asset_id, horizon, as_of`
- `idx_asset_scores_market_score`：`market, total_score`
- `idx_asset_scores_status`：`status`

### 7.6 signal_snapshots

信号快照表。信号是推荐的证据之一，不直接等于推荐。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `signal_id` | string | 主键 |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 代码 |
| `market` | string | 市场 |
| `horizon` | string | 周期 |
| `direction` | string | bullish、bearish、neutral、mixed |
| `score` | decimal | 信号分 |
| `confidence` | decimal | 置信度 |
| `rule_version` | string | 规则版本 |
| `status` | string | 状态 |
| `as_of` | datetime | 信号时间 |
| `payload` | JSON | 完整 SignalSnapshot |

索引：

- `idx_signals_asset_horizon_asof`：`asset_id, horizon, as_of`
- `idx_signals_market_direction`：`market, direction`
- `idx_signals_status`：`status`

## 8. 风险与推荐

### 8.1 risk_findings

风险发现表。保存系统规则和风险反驳 Agent 输出的风险。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `risk_id` | string | 主键 |
| `asset_id` | string | 资产 ID，可为空 |
| `scope` | string | asset、universe、portfolio |
| `risk_type` | string | valuation、drawdown、liquidity、leverage、data_quality 等 |
| `severity` | string | info、low、medium、high、critical |
| `score` | decimal | 风险分 |
| `title` | string | 风险标题 |
| `description` | text | 风险描述 |
| `as_of` | datetime | 风险时间 |
| `evidence_ids` | JSON | 证据 ID |
| `payload` | JSON | 完整 RiskFinding |

索引：

- `idx_risks_asset_asof`：`asset_id, as_of`
- `idx_risks_type_severity`：`risk_type, severity`
- `idx_risks_scope`：`scope`

### 8.2 recommendation_runs

一次推荐运行表。用于记录本次推荐的输入、策略和总输出。单市场推荐可以直接使用 `universe_id`，混合市场或多候选池推荐通过 `recommendation_run_universes` 关联多个候选池。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | string | 主键 |
| `universe_id` | string | 主候选池 ID，可为空 |
| `screening_id` | string | 初筛 ID |
| `strategy` | string | 推荐策略 |
| `market` | string | ashare、crypto_spot、crypto_future 或 mixed |
| `horizon` | string | 周期 |
| `limit` | int | 推荐数量 |
| `status` | string | 运行状态 |
| `started_at` | datetime | 开始时间 |
| `finished_at` | datetime | 结束时间 |
| `summary` | text | 中文摘要 |
| `payload` | JSON | 推荐运行完整上下文 |

索引：

- `idx_recommendation_runs_universe`：`universe_id`
- `idx_recommendation_runs_strategy_market`：`strategy, market`
- `idx_recommendation_runs_started_at`：`started_at`

### 8.3 recommendation_run_universes

推荐运行与候选池关联表。用于支持一次推荐同时读取多个候选池，例如 A 股沪深 300 + 数字货币主流币池。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 主键 |
| `run_id` | string | 推荐运行 ID |
| `universe_id` | string | 候选池 ID |
| `market` | string | 候选池市场 |
| `role` | string | primary、comparison、watchlist、excluded |
| `weight` | decimal | 混合推荐时该候选池权重 |
| `asset_count` | int | 参与推荐的标的数量 |
| `payload` | JSON | 候选池参与本次推荐的上下文 |

唯一约束：

- `run_id, universe_id`

索引：

- `idx_run_universes_run`：`run_id`
- `idx_run_universes_universe`：`universe_id`
- `idx_run_universes_market`：`market`

### 8.4 asset_recommendations

单标的推荐结果表。推荐榜直接读取这张表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `recommendation_id` | string | 主键 |
| `run_id` | string | 推荐运行 ID |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 代码 |
| `name` | string | 名称 |
| `market` | string | 市场 |
| `horizon` | string | 周期 |
| `action` | string | buy_candidate、watch、avoid 等 |
| `rank` | int | 推荐排名 |
| `total_score` | decimal | 综合分 |
| `confidence` | decimal | 置信度 |
| `conviction` | string | low、medium、high |
| `score_id` | string | 评分 ID |
| `factor_frame_id` | string | 因子 ID |
| `signal_ids` | JSON | 信号 ID |
| `risk_ids` | JSON | 风险 ID |
| `agent_analysis_item_ids` | JSON | Agent 分析明细 ID |
| `evidence_ids` | JSON | 证据 ID |
| `watch_conditions` | JSON | 观察条件，例如回踩、突破、资金费率回落 |
| `invalid_if` | JSON | 推荐失效条件 |
| `summary` | text | 推荐摘要 |
| `created_at` | datetime | 生成时间 |
| `payload` | JSON | 完整 AssetRecommendation |

索引：

- `idx_recommendations_run_rank`：`run_id, rank`
- `idx_recommendations_asset_created`：`asset_id, created_at`
- `idx_recommendations_market_action`：`market, action`
- `idx_recommendations_score`：`total_score`

### 8.5 agent_analysis_runs

Agent 分析运行审计表。记录一次 AgentGraph 或单个 Agent 的运行信息，不把自然语言输出直接当事实来源，但必须保留可审计轨迹。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_run_id` | string | 主键 |
| `run_id` | string | 推荐运行 ID |
| `agent_name` | string | Agent 名称，例如 technical_analyst、risk_rebuttal |
| `agent_role` | string | fundamental、technical、flow_derivatives、event、risk_rebuttal、decision |
| `model` | string | 使用的模型 |
| `model_version` | string | 模型版本或别名 |
| `input_ref` | string | 输入上下文引用，例如 run_id 或 payload hash |
| `input_summary` | text | 输入摘要 |
| `output_summary` | text | 输出摘要 |
| `status` | string | running、succeeded、failed、skipped |
| `started_at` | datetime | 开始时间 |
| `finished_at` | datetime | 结束时间 |
| `latency_ms` | int | 耗时 |
| `error_message` | text | 错误信息 |
| `payload` | JSON | 完整 Agent 输入输出、提示词版本和工具调用记录 |

索引：

- `idx_agent_runs_recommendation`：`run_id`
- `idx_agent_runs_agent_status`：`agent_name, status`
- `idx_agent_runs_started_at`：`started_at`

### 8.6 agent_analysis_items

Agent 单标的分析明细表。推荐卡片、风险反驳和中文解释报告优先读取这里的结构化摘要。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_analysis_item_id` | string | 主键 |
| `agent_run_id` | string | Agent 运行 ID |
| `run_id` | string | 推荐运行 ID |
| `asset_id` | string | 资产 ID |
| `symbol` | string | 代码 |
| `market` | string | 市场 |
| `agent_name` | string | Agent 名称 |
| `stance` | string | support、oppose、neutral、risk_only |
| `confidence` | decimal | 置信度 |
| `key_points` | JSON | 结构化要点 |
| `risk_ids` | JSON | 关联风险 ID |
| `evidence_ids` | JSON | 关联证据 ID |
| `summary` | text | 中文分析摘要 |
| `as_of` | datetime | 分析时间 |
| `payload` | JSON | 完整分析输出 |

唯一约束：

- `agent_run_id, asset_id, agent_name`

索引：

- `idx_agent_items_run_asset`：`run_id, asset_id`
- `idx_agent_items_asset_asof`：`asset_id, as_of`
- `idx_agent_items_agent_stance`：`agent_name, stance`

## 9. 回测与绩效

### 9.1 backtest_results

回测结果表。用于验证评分策略或候选 TopN 策略。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `backtest_id` | string | 主键 |
| `strategy` | string | 策略名称 |
| `universe_id` | string | 候选池 ID |
| `market` | string | 市场 |
| `horizon` | string | 周期 |
| `start_at` | datetime | 回测开始 |
| `end_at` | datetime | 回测结束 |
| `total_return` | decimal | 总收益 |
| `annual_return` | decimal | 年化收益 |
| `max_drawdown` | decimal | 最大回撤 |
| `turnover` | decimal | 换手率 |
| `status` | string | 状态 |
| `created_at` | datetime | 创建时间 |
| `payload` | JSON | 净值曲线、参数、持仓明细等 |

索引：

- `idx_backtests_strategy_market`：`strategy, market`
- `idx_backtests_universe`：`universe_id`
- `idx_backtests_created_at`：`created_at`

### 9.2 performance_reports

绩效报告表，保存 quantstats 统计结果和报告附件引用。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `performance_id` | string | 主键 |
| `backtest_id` | string | 回测 ID |
| `sharpe` | decimal | 夏普比率 |
| `sortino` | decimal | Sortino |
| `cagr` | decimal | 年化复合收益 |
| `max_drawdown` | decimal | 最大回撤 |
| `volatility` | decimal | 波动率 |
| `win_rate` | decimal | 周期胜率 |
| `html_report_path` | string | HTML 报告路径 |
| `status` | string | 状态 |
| `created_at` | datetime | 创建时间 |
| `payload` | JSON | 完整绩效报告 |

索引：

- `idx_performance_backtest`：`backtest_id`
- `idx_performance_created_at`：`created_at`

## 10. 推荐主链路查询

### 10.1 获取最新推荐榜

常用查询条件：

- `market`
- `universe_id`
- `strategy`
- `horizon`
- `run_id`

推荐读取顺序：

```text
recommendation_runs
  -> recommendation_run_universes
  -> asset_recommendations
  -> asset_scores
  -> agent_analysis_items
  -> evidence
```

### 10.2 个股或单币分析页

推荐读取顺序：

```text
assets
  -> market_bars
  -> indicator_frames
  -> factor_frames
  -> asset_scores
  -> signal_snapshots
  -> risk_findings
  -> agent_analysis_items
  -> asset_recommendations
  -> evidence
```

如果是 A 股，补充：

```text
fundamental_snapshots
capital_flow_snapshots
event_records
```

如果是数字货币，补充：

```text
crypto_derivative_snapshots
event_records
```

## 11. 建表优先级

### M0：跑通推荐主链路

优先实现这些表：

- `assets`
- `asset_universes`
- `asset_universe_members`
- `raw_records`
- `market_bars`
- `market_calendars`
- `indicator_frames`
- `factor_frames`
- `screening_results`
- `screening_result_items`
- `asset_scores`
- `signal_snapshots`
- `risk_findings`
- `recommendation_runs`
- `recommendation_run_universes`
- `asset_recommendations`
- `agent_analysis_runs`
- `agent_analysis_items`
- `evidence`

M0 目标：

- 能构建 A 股和数字货币候选池。
- 能写入标准行情。
- 能保存指标、因子、逐标的筛选、评分、风险反驳和推荐结果。
- 能审计每个 Agent 的输入、输出、状态和单标的分析摘要。
- 能生成 `result.json` 和 `report.md`。

### M1：增强分析质量

第二阶段实现这些表：

- `fundamental_snapshots`
- `capital_flow_snapshots`
- `crypto_derivative_snapshots`
- `event_records`
- `backtest_results`
- `performance_reports`

M1 目标：

- A 股补充财务、估值和资金流。
- 数字货币补充资金费率、未平仓量、多空比。
- 推荐补充更完整的事件、财务、资金流、衍生品和回测依据。
- 推荐榜可以展示回测和绩效依据。

### M2：交易和账户扩展

第三阶段再考虑：

- `account_snapshots`
- `positions`
- `order_drafts`
- `execution_audit_logs`

M2 目标：

- 支持持仓辅助分析。
- 支持订单草案。
- 保持推荐和真实下单分离。

## 12. 后续实现建议

第一版使用 SQLAlchemy 2.x ORM 和 Alembic 迁移。

数据库部署建议：

```text
PostgreSQL 16+
TimescaleDB 2.x
```

Alembic 迁移中需要先启用扩展：

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

然后再创建普通表和 hypertable。注意：TimescaleDB hypertable 的唯一约束必须包含时间列，因此：

- `market_bars` 的唯一约束使用 `asset_id, timeframe, timestamp, source, adjustment`。
- `crypto_derivative_snapshots` 的唯一约束使用 `asset_id, as_of, source`。
- 不要为 hypertable 设计不包含时间列的单列主键。
- 需要被唯一约束覆盖的可选文本字段使用空字符串默认值，不使用 `NULL`。

建议目录：

```text
src/finance_agent/storage/
  db.py
  orm.py
  repositories.py
  migrations/
```

实现顺序：

1. 先建立 M0 表的 ORM 模型和 Alembic 迁移。
2. 建立 `AssetRepository`、`UniverseRepository`、`MarketDataRepository`。
3. 建立 `IndicatorRepository`、`FactorRepository`、`ScreeningRepository`、`ScoreRepository`。
4. 建立 `RiskRepository`、`AgentAnalysisRepository`、`RecommendationRepository`。
5. 跑通一次 `recommend assets` 的写入、读取、证据追溯和中文报告生成。
6. 再补 M1 专属快照、事件、回测和绩效表。
