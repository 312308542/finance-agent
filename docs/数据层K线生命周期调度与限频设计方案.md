# 数据层 K 线生命周期调度与限频设计方案

更新日期：2026-06-04

## 1. 背景

当前 A 股数据层已经具备基础采集能力，但日 K 线仍然容易被误用为“每小时全市场刷新一次”的高频任务。这种方式会带来几个问题：

1. 日 K 线本质是交易日级别数据，盘中只能得到未收盘的临时值，正式可用于因子、风控和推荐的应该是收盘后的最终日 K。
2. 全市场逐股请求会放大 AKShare 上游源的网络波动、限流、断连和超时问题，导致一次任务可能跑 7 到 8 小时仍不完整。
3. 高频任务和低频任务混在一个 `ashare.bars.1d` 中，会让系统无法区分“历史补齐”“盘中观察”“收盘落库”“凌晨修正”。
4. 推荐链路需要稳定、闭合、可解释的数据门禁，不能把未闭合日 K 或部分失败的数据直接推入正式推荐。

本方案把 A 股日 K 从单一补采任务拆成完整的数据生命周期，并将 AKShare 访问频率控制做成可配置、可观测、可退避的策略层。

## 2. 设计目标

- 数据完整：初始化阶段补齐历史 K 线，日常阶段只补缺口、失败和近期修正。
- 数据准确：盘中日 K 标记为 `partial`，收盘后最终日 K 标记为 `available` 和 `is_closed=true`。
- 数据实时：盘中观察依赖实时行情接口，不再依赖全市场逐股刷新日 K。
- 低干扰：凌晨执行复权修正、失败重试和补漏，减少白天对上游接口的压力。
- 限频友好：按数据源、host、接口和任务类型控制并发、间隔、退避和熔断。
- 可恢复：每个资产、数据域、周期和 provider 都有水位与失败重试记录。
- 可观测：Redis 任务进度展示总量、完成量、失败量、当前批次、吞吐量、并发数和最近错误。
- 推荐可信：因子、信号、推荐只消费闭合日 K；盘中数据只进入观察和提醒。

## 3. 外部资料依据

AKShare 官方数据字典显示：

- `stock_zh_a_spot_em` 是东方财富沪深京 A 股实时行情接口，单次返回所有沪深京 A 股上市公司的实时行情数据，适合盘中观察和资产池刷新。
- `stock_zh_a_hist` 是东方财富历史行情接口，按指定股票、周期和日期范围返回日频历史行情，文档说明当日收盘价应在收盘后获取。
- `stock_zh_a_hist_tx` 是腾讯历史行情接口，同样按指定股票和日期范围返回日频数据，文档也说明当日收盘价应在收盘后获取。
- `stock_news_em` 是东方财富个股新闻接口，单次返回指定 symbol 当日最近 100 条新闻资讯数据。

AKShare README 的声明中说明 AKShare 数据用于学术研究和参考，部分接口可能因不可控因素被移除；官方文档没有给出统一 QPS 或每日请求量配额。因此系统不能假设“AKShare 本身没有限制就可以无限并发”，应按上游源实际表现做保守限频和动态退避。

参考链接：

- https://akshare.akfamily.xyz/data/stock/stock.html
- https://github.com/akfamily/akshare/blob/main/README.md
- https://github.com/akfamily/akshare/issues/5762

## 4. 当前系统基础

当前系统已有以下可复用基础：

| 模块 | 当前能力 | 方案中的定位 |
|---|---|---|
| `market_bars` | 已有 `is_closed`、`status`、`adjustment`、`source`、`raw_record_id` | 可区分未闭合、闭合、修正和异常 K 线 |
| `realtime_quote_snapshots` | 保存实时行情快照 | 盘中观察主数据源 |
| `data_sync_watermarks` | 记录资产、数据域、周期、provider 的成功水位和失败重试 | 增量调度、失败退避、补漏依据 |
| `SourceRateLimiter` | 已有按 source key 的并发和最小间隔控制 | 扩展为按接口、host 和任务类型配置 |
| `BaseDataScheduler` | 支持 collection、data_quality_refresh、recommendation_pipeline、并发任务、状态文件和进度记录 | 增加日历时间型任务和任务依赖 |
| Redis 任务进度 | 展示任务状态、阶段、日志、计数 | 做任务监控页面的数据来源 |

## 5. 总体链路

```text
低频资产池刷新
  -> assets / asset_profiles / provider_mappings / universe_members
  -> 历史 K 初始化或增量补齐
  -> 收盘最终日 K
  -> 数据质量检查
  -> 指标计算
  -> 因子计算
  -> 信号/风险
  -> 推荐运行
  -> Agent 工具读取推荐、因子、风险、新闻和记忆

盘中实时观察
  -> realtime_quote_snapshots
  -> 观察池提醒 / 风险触发 / Agent 问答上下文
  -> 不触发正式推荐

凌晨修正
  -> 最近 N 个交易日复权和漏数修正
  -> 更新水位和数据质量
  -> 必要时触发指标、因子和推荐重算
```

## 6. A 股日 K 生命周期

### 6.1 历史初始化

任务名建议：`ashare.bars.1d.bootstrap`

用途：

- 系统首次上线或清库重建后补齐全市场历史 K 线。
- 新增资产进入资产池后补齐该资产历史 K。
- 用户手动触发全量重建时使用。

执行策略：

- 默认不放入高频 loop，也不每小时触发。
- 支持手动触发、低峰定时触发或补缺口触发。
- 每个资产按水位判断是否已覆盖足够历史，MVP 默认使用 `lookback=10y`。
- 分批执行，批内低并发，失败写入 `data_sync_watermarks.next_retry_at`。

推荐配置：

| 参数 | 建议值 | 说明 |
|---|---:|---|
| `max_workers` | 2 | 全历史窗口请求较重，低并发更稳 |
| `batch_size` | 100 到 200 | 控制单批资产数，不截断总资产池 |
| `min_interval_seconds` | 0.5 到 1.5 | 每个 K 线接口请求间隔 |
| `timeout_seconds` | 30 到 60 | 单资产请求超时 |
| `retry_policy` | 指数退避 | 网络错误不阻塞全批次 |

### 6.2 盘中观察

任务名建议：`ashare.realtime_quotes`

用途：

- 刷新最新价、涨跌幅、成交量额、换手率、市值、交易状态。
- 供观察池、风控提醒、Agent 问答和盘中摘要使用。

执行策略：

- 使用全市场实时行情接口，避免逐股请求。
- 交易时段内每 1 到 5 分钟运行。
- 非交易时段自动降频或停止。
- 不触发正式推荐，只触发观察提醒和状态快照。

推荐频率：

| 场景 | 频率 |
|---|---:|
| 集合竞价和开盘前后 | 60 秒 |
| 正常交易时段 | 120 到 300 秒 |
| 午间休市 | 10 到 30 分钟 |
| 收盘后 | 停止或 30 分钟低频 |

### 6.3 午盘临时日 K

任务名建议：`ashare.bars.1d.midday_partial`

用途：

- 在午盘后记录当日上午形成的临时日 K，用于观察和盘中复盘。
- 不作为正式因子和推荐依据。

执行时间：

- 交易日 `11:40` 到 `12:10`。

写库规则：

- `market_bars.is_closed=false`
- `market_bars.status='partial'`
- `payload.session='midday'`
- `payload.trading_date=YYYY-MM-DD`

消费规则：

- 可供 Agent 回答“上午走势如何”“盘中观察池有哪些异动”。
- 不进入正式 `IndicatorService`、`FactorService` 和推荐运行。

### 6.4 收盘最终日 K

任务名建议：`ashare.bars.1d.close_final`

用途：

- 获取交易日最终日 K。
- 作为指标、因子、信号和推荐的正式输入。

执行时间：

- 交易日 `15:40` 到 `16:30`。
- 如果上游源延迟，可在 `17:30` 做一次轻量补跑。

写库规则：

- `market_bars.is_closed=true`
- `market_bars.status='available'`
- 同一资产、周期、日期、复权方式用 upsert 覆盖午盘 partial。
- 成功后更新 `data_sync_watermarks.watermark_at`。

后置触发：

```text
close_final 成功
  -> data_quality_refresh
  -> indicator_refresh
  -> factor_refresh
  -> signal_risk_refresh
  -> recommendation_pipeline
```

### 6.5 凌晨修正

任务名建议：`ashare.bars.1d.revision`

用途：

- 次日修正、复权变化、失败补漏、数据源延迟修复。
- 将前一交易日和最近几个交易日的 K 线重新校验。

执行时间：

- 每天凌晨 `02:00` 到 `03:30`。
- 只处理最近 3 到 7 个交易日，以及失败水位到期的资产。

为什么放凌晨：

- A 股已收盘，上游源数据更稳定。
- 避免和白天实时行情、新闻、资金流任务抢网络和 provider 配额。
- 对复权变化、公告导致的数据修正更友好。

写库规则：

- 如果数据发生变化，更新 `market_bars` 并记录 `payload.revision_reason`。
- 如果仅补漏，更新水位和 `raw_records`。
- 如果修正影响指标和因子，标记下游重算范围。

## 7. 任务拆分建议

| 任务 | 类型 | 建议触发 | 数据范围 | 是否正式输入 | 备注 |
|---|---|---|---|---|---|
| `ashare.universe.all` | 资产池 | 每日 06:00 或低频 | 全 A、指数、行业、概念、热点种子 | 是 | 修复资产名称、交易所、币种等稳定字段 |
| `ashare.realtime_quotes` | 实时行情 | 交易时段 1 到 5 分钟 | 全市场接口 | 否 | 盘中观察主入口 |
| `ashare.bars.1d.bootstrap` | 历史 K | 手动或低峰 | 缺历史覆盖资产 | 是 | 初始化和大缺口补齐 |
| `ashare.bars.1d.midday_partial` | 临时日 K | 交易日午盘后 | 重点资产或全市场低频 | 否 | `is_closed=false` |
| `ashare.bars.1d.close_final` | 最终日 K | 交易日收盘后 | 全市场或缺口资产 | 是 | 触发正式分析链路 |
| `ashare.bars.1d.revision` | 日 K 修正 | 每日凌晨 | 最近 3 到 7 交易日、失败资产 | 是 | 复权、补漏、失败重试 |
| `ashare.fundamentals` | 基本面/估值 | 每日凌晨或每 12 小时低频 | 全市场或报告期变化资产 | 是 | 不适合盘中高频 |
| `ashare.capital_flow` | 资金流 | 15 到 30 分钟 | 榜单和重点资产 | 是/观察 | 需要独立限频 |
| `ashare.events` | 新闻公告 | 5 到 15 分钟 | 市场事件、重点个股新闻 | 是/观察 | 原文抓取异步执行 |
| `ashare.risk_sentiment` | 风险情绪 | 5 到 15 分钟 | 停复牌、涨跌停、龙虎榜、大宗等 | 是/观察 | 列表型接口优先 |

## 8. 调度模型改造

当前 `interval_seconds` 只能表达固定间隔。为了支持凌晨执行、收盘后执行和交易时段执行，建议扩展调度字段：

```json
{
  "name": "ashare.bars.1d.revision",
  "job_type": "collection",
  "schedule_type": "daily_time",
  "run_at": ["02:10"],
  "timezone": "Asia/Shanghai",
  "trading_day_policy": "any_day",
  "market": "ashare",
  "params": {
    "sync_task_type": "market_bars_revision",
    "timeframe": "1d",
    "lookback": "7d",
    "only_failed_or_stale": true,
    "include_adjustment_check": true
  }
}
```

建议支持的 `schedule_type`：

| 类型 | 含义 | 示例 |
|---|---|---|
| `interval` | 固定间隔 | 实时行情每 300 秒 |
| `daily_time` | 每日指定时间 | 凌晨修正 02:10 |
| `trading_session` | 交易时段窗口 | 09:30 到 11:30，13:00 到 15:00 |
| `manual` | 手动触发 | 历史初始化 |
| `after_success` | 依赖上游成功后触发 | close_final 后跑指标和推荐 |

建议支持的 `trading_day_policy`：

| 策略 | 含义 |
|---|---|
| `trading_day_only` | 仅交易日执行 |
| `non_trading_day_only` | 仅非交易日执行 |
| `any_day` | 每天都可执行 |
| `previous_trading_day_required` | 需要存在最近交易日 |

## 9. AKShare 限频和退避策略

### 9.1 基本原则

AKShare 没有官方统一 QPS 额度，实际限制来自东方财富、腾讯、新浪、同花顺、交易所等上游源。系统应采用“保守默认值 + 运行时自适应”的策略。

核心原则：

- 全市场接口优先，逐股接口谨慎。
- K 线、新闻正文、公告详情等逐标的接口必须限频。
- 网络错误、远端断连、空响应和 403/429 都要进入退避。
- 失败不阻塞整批任务，写水位后继续处理其它资产。
- 同一 host 和同一接口共享限频预算。

### 9.2 默认限频建议

| Source Key | 数据源 | 默认并发 | 最小间隔 | 适用任务 |
|---|---|---:|---:|---|
| `stock_zh_a_spot_em` | 东方财富实时全市场 | 1 | 30 到 60 秒 | 实时行情 |
| `stock_zh_a_hist_tx` | 腾讯日 K | 2 | 0.5 到 1.5 秒 | 历史 K、收盘 K、凌晨修正 |
| `stock_zh_a_hist` | 东方财富日 K | 1 到 2 | 1 到 3 秒 | 备用 K 线源 |
| `stock_news_em` | 东方财富个股新闻 | 1 到 2 | 2 到 5 秒 | 重点资产新闻 |
| `stock_notice_report` | 公告列表 | 1 | 3 到 5 秒 | 公告 |
| `fundamental_em` | 东方财富基本面 | 1 到 2 | 1 到 3 秒 | 基本面和估值 |
| `ccxt_binance_fetch_ohlcv` | Binance K 线 | 3 | 0.05 到 0.2 秒 | 数字货币 K 线 |

说明：

- 当前代码中的 `stock_zh_a_hist_tx` 间隔偏短，可以作为测试环境配置；生产建议提高到 0.5 秒以上。
- 新闻正文二次抓取比新闻列表更容易触发限制，建议单独 source key，不和 `stock_news_em` 共用预算。
- 如果出现 `curl(56)`、`curl(28)`、`ConnectTimeout`，应动态降低并发并增加最小间隔。

### 9.3 错误分级

| 错误类型 | 处理方式 | Retry 建议 |
|---|---|---|
| `curl(56)` 连接被关闭 | 记录失败水位，降低该 source 并发 | 15 分钟后重试 |
| `curl(28)` 超时 | 记录失败水位，增加 timeout 或退避 | 15 到 30 分钟 |
| 403/429 | 进入 provider 熔断 | 30 到 60 分钟 |
| 空数据但非停牌 | 标记疑似限流或上游异常 | 下一轮或凌晨修正 |
| 单资产格式异常 | 记录资产级失败 | 不影响其它资产 |
| 批次数据库异常 | rollback 当前事务，使用独立水位事务记录失败 | 继续下一批 |

### 9.4 自适应退避

建议为每个 source key 维护运行期状态：

```json
{
  "source_key": "stock_zh_a_hist_tx",
  "window_seconds": 300,
  "success_count": 420,
  "failure_count": 18,
  "timeout_count": 9,
  "disconnect_count": 6,
  "rate_limited_count": 3,
  "effective_max_concurrency": 1,
  "effective_min_interval_seconds": 2.0,
  "next_recover_at": "2026-06-04T03:20:00+08:00"
}
```

退避规则：

- 5 分钟窗口内失败率超过 10%，并发减半，间隔翻倍。
- 连续 3 次 403/429，source 熔断 30 分钟。
- 连续 3 次成功窗口后，逐步恢复到配置上限。
- 退避状态写 Redis，水位和最终失败写数据库。

## 10. 数据完整性和消费门禁

### 10.1 K 线状态

| 字段 | 盘中临时 K | 收盘最终 K | 凌晨修正 K |
|---|---|---|---|
| `is_closed` | `false` | `true` | `true` |
| `status` | `partial` | `available` | `available` 或 `revised` |
| `payload.session` | `midday` 或 `intraday` | `close` | `revision` |
| 推荐可消费 | 否 | 是 | 是 |

### 10.2 推荐门禁

正式推荐运行必须满足：

- 最近交易日存在 `is_closed=true` 的日 K。
- `market_bars` 覆盖数达到 `min_bars`。
- 指标覆盖率达到配置阈值。
- 因子覆盖率达到配置阈值。
- 缺失因子组低于阈值。
- 失败水位中没有大量未恢复的核心数据域。

盘中 Agent 问答可以消费：

- `realtime_quote_snapshots`
- `market_bars.status='partial'`
- 最新新闻、公告、资金流和风险情绪

但回答中必须标注“盘中观察数据，不是收盘后正式推荐”。

## 11. 与新闻和事件源的关系

新闻是实时性数据，不应拖慢主采集任务。

建议拆成三层：

1. 新闻列表层：快速入库标题、摘要、发布时间、来源、链接、关联资产。
2. 原文抓取层：异步低并发二次抓取正文，失败可重试。
3. 清洗分析层：提取主题、情绪、事件类型、影响方向、关联资产和置信度。

保留策略：

- 原始新闻可以只保存近 30 到 90 天。
- 清洗后的事件摘要、情绪和资产影响记录保留更长时间。
- 已进入推荐证据或 Agent 记忆的新闻应保留引用快照。

## 12. 配置示例

### 12.1 日 K 生命周期任务

```json
{
  "jobs": [
    {
      "name": "ashare.bars.1d.bootstrap",
      "job_type": "collection",
      "enabled": false,
      "schedule_type": "manual",
      "market": "ashare",
      "params": {
        "sync_task_type": "market_bars_full_history_backfill",
        "timeframe": "1d",
        "lookback": "10y",
        "symbol_source": "market_assets",
        "batch_size": 200,
        "max_workers": 2
      }
    },
    {
      "name": "ashare.bars.1d.midday_partial",
      "job_type": "collection",
      "enabled": true,
      "schedule_type": "daily_time",
      "run_at": ["11:45"],
      "timezone": "Asia/Shanghai",
      "trading_day_policy": "trading_day_only",
      "market": "ashare",
      "params": {
        "sync_task_type": "market_bars_midday_partial",
        "timeframe": "1d",
        "symbol_source": "priority_assets",
        "is_closed": false,
        "status": "partial",
        "max_workers": 2
      }
    },
    {
      "name": "ashare.bars.1d.close_final",
      "job_type": "collection",
      "enabled": true,
      "schedule_type": "daily_time",
      "run_at": ["15:50", "17:30"],
      "timezone": "Asia/Shanghai",
      "trading_day_policy": "trading_day_only",
      "market": "ashare",
      "params": {
        "sync_task_type": "market_bars_close_final",
        "timeframe": "1d",
        "symbol_source": "market_assets",
        "is_closed": true,
        "status": "available",
        "batch_size": 200,
        "max_workers": 2
      }
    },
    {
      "name": "ashare.bars.1d.revision",
      "job_type": "collection",
      "enabled": true,
      "schedule_type": "daily_time",
      "run_at": ["02:10"],
      "timezone": "Asia/Shanghai",
      "trading_day_policy": "any_day",
      "market": "ashare",
      "params": {
        "sync_task_type": "market_bars_revision",
        "timeframe": "1d",
        "lookback": "7d",
        "only_failed_or_stale": true,
        "include_adjustment_check": true,
        "batch_size": 200,
        "max_workers": 2
      }
    }
  ]
}
```

### 12.2 限频策略

```json
{
  "rate_policies": {
    "stock_zh_a_hist_tx": {
      "max_concurrency": 2,
      "min_interval_seconds": 1.0,
      "timeout_seconds": 45,
      "backoff": {
        "failure_rate_threshold": 0.1,
        "cooldown_seconds": 900,
        "max_interval_seconds": 10
      }
    },
    "stock_news_em": {
      "max_concurrency": 1,
      "min_interval_seconds": 3.0,
      "timeout_seconds": 30
    },
    "stock_news_article": {
      "max_concurrency": 1,
      "min_interval_seconds": 5.0,
      "timeout_seconds": 30
    }
  }
}
```

## 13. 接口和监控建议

### 13.1 Redis 进度结构

任务运行期进度建议继续写 Redis，不写数据库。

```json
{
  "task_id": "ashare.bars.1d.close_final:20260604",
  "task_name": "A 股收盘最终日 K",
  "status": "running",
  "phase": "fetch_market_bars",
  "total": 5530,
  "completed": 3524,
  "running": 2,
  "remaining": 1704,
  "failed": 12,
  "retry_waiting": 34,
  "max_workers": 2,
  "throughput_per_minute": 84,
  "started_at": "2026-06-04T15:50:00+08:00",
  "estimated_finished_at": "2026-06-04T17:02:00+08:00"
}
```

### 13.2 前端任务监控展示

任务详情页应展示：

- 总同步数量、已完成、处理中、剩余、失败、等待重试。
- 当前阶段，例如资产池读取、批次请求、写库、水位记录、质量检查。
- 当前并发数和吞吐量。
- 最近错误列表，默认省略，点击后弹出完整错误。
- 数据源退避状态，例如腾讯 K 线已降并发、东方财富新闻进入冷却。

## 14. 推荐落地顺序

优先级建议：

1. 先补调度语义：支持 `daily_time`、交易日判断、凌晨任务。
2. 再拆日 K 任务：bootstrap、midday_partial、close_final、revision。
3. 接着完善数据消费门禁：推荐只读 `is_closed=true`。
4. 然后强化限频：配置化 source policy、自适应退避、Redis source 状态。
5. 最后优化监控 UI：展示各任务阶段、吞吐量、退避状态和完整错误弹窗。

## 15. 验收标准

| 验收项 | 标准 |
|---|---|
| 历史初始化 | 清库后能补齐全市场历史 K，失败资产写水位并可重试 |
| 午盘任务 | 只写 `partial`，不触发正式推荐 |
| 收盘任务 | 写 `is_closed=true`，触发质量、指标、因子、推荐链路 |
| 凌晨修正 | 每日 02:10 执行最近 7 天复权、补漏和失败重试 |
| 限频 | 同一数据源并发和请求间隔可配置，失败率升高时自动退避 |
| 任务监控 | 可以看到每个任务总量、完成量、失败量、吞吐量、并发和完整错误 |
| 推荐问答 | Agent 能区分盘中观察数据和收盘正式推荐 |

## 16. 开发进度表

| 编号 | 模块 | 任务 | 状态 | 验收标准 | 备注 |
|---|---|---|---|---|---|
| KLS-001 | 设计文档 | 编写 K 线生命周期和限频设计方案 | 已完成 | 文档落地，包含凌晨修正和频率控制方案 | 本文档 |
| KLS-002 | 配置模型 | 扩展调度配置，支持 `schedule_type`、`run_at`、`timezone`、`trading_day_policy` | 已完成 | JSON 配置可解析、预览、导出，旧配置兼容 | 已支持配置导出到 scheduler payload |
| KLS-003 | 调度器 | `BaseDataScheduler` 支持 daily_time、交易日窗口、manual 和 after_success | 已完成 | 单测覆盖每日时间、非交易日跳过、依赖触发 | 已支持 daily/manual/after_success 调度语义 |
| KLS-004 | 日 K 任务拆分 | 新增 bootstrap、midday_partial、close_final、revision 四类任务 | 已完成 | 四类任务能分别写入正确状态和水位 | A 股日 K 已按生命周期拆分 |
| KLS-005 | K 线写库 | normalizer 支持 `is_closed` 和 `status` 参数，partial 可被 close_final 覆盖 | 已完成 | 同一交易日 partial 到 final 状态正确演进 | 已覆盖 market_bars upsert 回归测试 |
| KLS-006 | 水位和补漏 | revision 根据最近交易日、失败水位和缺口选择资产 | 已完成 | 失败资产到期后被重试，成功后清理错误状态 | 已接入 `data_sync_watermarks` |
| KLS-007 | 限频配置 | 将 `SourceRateLimiter` 策略从硬编码扩展到配置文件 | 已完成 | source policy 可配置，默认值保守 | `rate_policies` 已随同步配置导出 |
| KLS-008 | 自适应退避 | Redis 记录 source 运行窗口，失败率高时自动降并发、增间隔 | 已完成 | curl(56)、curl(28)、403/429 都能触发退避 | 运行态写 Redis，前端可展示 source 状态 |
| KLS-009 | 数据质量 | 区分 partial K 和 final K，质量检查只统计闭合日 K | 已完成 | 推荐门禁不会把盘中 partial 当正式 K | 质量服务只统计 `is_closed=true` 且可用状态 |
| KLS-010 | 推荐链路 | close_final 成功后触发指标、因子、信号和推荐，midday 不触发 | 已完成 | 收盘后推荐可自动刷新，盘中只做观察 | `quality.ashare` 和推荐任务依赖 close_final 链路 |
| KLS-011 | 新闻链路 | 新闻列表、原文抓取、清洗分析分层；原文抓取低并发异步 | 已完成 | 新闻主链路不等待原文，原文失败可重试 | 已拆出 `ashare.news_articles` 并接入限频 |
| KLS-012 | 任务监控 | 前端展示阶段进度、并发、吞吐量、退避状态和完整错误弹窗 | 已完成 | 任务详情能看到每个任务内部进度和错误详情 | 已展示 source 退避状态，日志支持点击弹窗查看完整内容 |
| KLS-013 | 测试验证 | 单测、集成测试、一次真实小批量验证 | 已完成 | pytest 通过，真实小批量可落库、可修正、可监控 | 已完成单标的 `000001` K 线小批量落库和水位核验 |

## 17. 后续记录区

| 日期 | 记录人 | 内容 | 结果 |
|---|---|---|---|
| 2026-06-04 | Codex | 建立设计方案和开发进度表 | 已完成 |
| 2026-06-04 | Codex | 完成 KLS-002 到 KLS-013：调度语义、日 K 生命周期、水位补漏、限频退避、质量门禁、推荐依赖、新闻原文补抓和任务监控 UI | 已完成；`pytest` 91 项通过，前端任务监控测试和构建通过 |
| 2026-06-04 | Codex | 执行 Alembic `20260604_0013`，创建 `data_sync_watermarks` 表并完成 `000001` 单标的收盘日 K 小批量验证 | 已完成；水位状态 `available`，闭合可用日 K 记录数 1 |
