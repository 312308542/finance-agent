# 可交易资产池与观察链路重构实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 按 `docs/superpowers/specs/2026-06-09-可交易资产池与观察链路重构设计.md` 将资产准入、10 年日 K 初始化、日常维护、推荐研究池和事件重点池拆清楚，并通过测试验证。

**架构：** 新增统一资产准入服务，A 股逐股任务统一使用可交易主板过滤；推荐自动同步改写系统研究跟踪池；事件新闻任务通过动态优先级解析器生成逐股名单，不再直接把所有 active 观察项当新闻重点池。

**技术栈：** Python、SQLAlchemy ORM、pytest、现有 `watchlists` / `watchlist_items` / `asset_recommendations` / Redis 任务进度结构。

---

## 文件结构

- 创建：`src/finance_agent/application/asset_eligibility_service.py`
  - 集中定义资产准入规则、可交易 A 股主板判断、可交易基金判断和资产列表过滤。
- 创建：`src/finance_agent/application/event_priority_service.py`
  - 根据用户观察池、系统研究跟踪池和最近推荐计算事件重点资产名单。
- 修改：`scripts/data/collect_base_data.py`
  - A 股 K 线、基本面、新闻重点名单使用统一准入服务和事件重点服务。
- 修改：`src/finance_agent/scheduler/base_data_scheduler.py`
  - 默认推荐自动同步目标从推荐观察池改为系统研究跟踪池，并调整名称。
- 修改：`src/finance_agent/data/sync_config.py`
  - 推荐任务导出的 `watchlist_id` 指向系统研究跟踪池。
- 修改：`src/finance_agent/agents/personal_assistant.py`
  - 推荐入池事件语义从 `recommendation_intake` 调整为研究跟踪入池，保留兼容字段。
- 修改：`src/finance_agent/application/watchlist_service.py`
  - 增加研究跟踪池常量和轻量辅助方法。
- 测试：`tests/test_asset_eligibility_service.py`
- 测试：`tests/test_event_priority_service.py`
- 测试：`tests/test_base_data_scheduler_analytics.py`
- 测试：`tests/test_data_sync_config.py`
- 测试：`tests/test_stock_news_event_source.py`

## 任务 1：统一资产准入服务

**文件：**
- 创建：`src/finance_agent/application/asset_eligibility_service.py`
- 测试：`tests/test_asset_eligibility_service.py`

- [x] **步骤 1：编写失败的测试**

测试应覆盖：

```python
from types import SimpleNamespace

from finance_agent.application.asset_eligibility_service import (
    TradeableAssetEligibilityService,
    is_tradeable_ashare_symbol,
)


def test_tradeable_ashare_symbol_allows_main_board_only() -> None:
    assert is_tradeable_ashare_symbol("000001") is True
    assert is_tradeable_ashare_symbol("600519") is True
    assert is_tradeable_ashare_symbol("300750") is False
    assert is_tradeable_ashare_symbol("688363") is False
    assert is_tradeable_ashare_symbol("873124") is False


def test_filter_tradeable_assets_keeps_ashare_main_board_and_funds() -> None:
    assets = [
        SimpleNamespace(asset_id="ashare:000001", market="ashare", symbol="000001", asset_type="stock", tradable=True),
        SimpleNamespace(asset_id="ashare:300750", market="ashare", symbol="300750", asset_type="stock", tradable=True),
        SimpleNamespace(asset_id="fund:510300", market="fund", symbol="510300", asset_type="etf", tradable=True),
        SimpleNamespace(asset_id="index:000300", market="index", symbol="000300", asset_type="index", tradable=True),
    ]

    selected = TradeableAssetEligibilityService().filter_tradeable_assets(assets)

    assert [asset.asset_id for asset in selected] == ["ashare:000001", "fund:510300"]
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_asset_eligibility_service.py -q`

预期：导入 `asset_eligibility_service` 失败。

- [x] **步骤 3：实现最少代码**

实现：

- `is_tradeable_ashare_symbol(symbol: str) -> bool`
- `TradeableAssetEligibilityService.is_tradeable_asset(asset: Any) -> bool`
- `TradeableAssetEligibilityService.filter_tradeable_assets(assets: Iterable[Any]) -> list[Any]`
- `TradeableAssetEligibilityService.filter_tradeable_ashare_symbols(symbols: Iterable[Any]) -> list[str]`

- [x] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_asset_eligibility_service.py -q`

预期：全部通过。

## 任务 2：A 股数据任务使用可交易资产池

**文件：**
- 修改：`scripts/data/collect_base_data.py`
- 测试：`tests/test_base_data_scheduler_analytics.py`
- 测试：`tests/test_ashare_market_bars_lifecycle.py`

- [x] **步骤 1：编写失败的测试**

新增或调整测试，确认：

- `batch_ashare_symbols` 只返回 A 股主板。
- `batch_ashare_fundamental_symbols` 只返回 A 股主板。
- 10 年日 K 初始化在 `market_assets` 模式下不截断资产全集，但会跳过非主板。

示例断言：

```python
assert symbols == ["000001", "002594", "600519"]
assert "300750" not in symbols
assert "688363" not in symbols
```

- [x] **步骤 2：运行测试验证失败或确认现有保护**

运行：

`.venv\Scripts\python.exe -m pytest tests/test_base_data_scheduler_analytics.py::test_ashare_market_bars_backfill_runs_all_market_assets_in_batches tests/test_ashare_market_bars_lifecycle.py -q`

预期：新增断言在未接入统一服务前失败，或暴露仍有散落过滤。

- [x] **步骤 3：接入统一准入服务**

将 `batch_ashare_symbols` 和 `batch_ashare_fundamental_symbols` 中散落的主板过滤替换为 `TradeableAssetEligibilityService`。

- [x] **步骤 4：运行测试验证通过**

运行：

`.venv\Scripts\python.exe -m pytest tests/test_asset_eligibility_service.py tests/test_base_data_scheduler_analytics.py::test_ashare_market_bars_backfill_runs_all_market_assets_in_batches tests/test_ashare_market_bars_lifecycle.py -q`

## 任务 3：推荐自动同步改为系统研究跟踪池

**文件：**
- 修改：`src/finance_agent/data/sync_config.py`
- 修改：`src/finance_agent/scheduler/base_data_scheduler.py`
- 修改：`src/finance_agent/agents/personal_assistant.py`
- 修改：`src/finance_agent/application/watchlist_service.py`
- 测试：`tests/test_data_sync_config.py`
- 测试：`tests/test_base_data_scheduler_analytics.py`

- [x] **步骤 1：编写失败的测试**

测试应确认：

```python
assert jobs["analytics.recommendations.ashare.all_a"]["params"]["watchlist_id"] == (
    "watchlist:default-owner:ashare:research"
)
assert jobs["analytics.recommendations.ashare.all_a"]["params"]["auto_sync_watchlist"] is True
```

调度器同步时创建的观察池名称应为 `A 股系统研究跟踪池`，purpose 为 `system_research_pool`。

- [x] **步骤 2：运行测试验证失败**

运行：

`.venv\Scripts\python.exe -m pytest tests/test_data_sync_config.py::test_recommendation_jobs_enable_default_watchlist_intake tests/test_base_data_scheduler_analytics.py::test_recommendation_job_passes_watchlist_intake_options -q`

预期：仍指向旧 `recommendations` 观察池而失败。

- [x] **步骤 3：实现研究跟踪池常量和同步目标**

实现：

- `DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID`
- `default_research_watchlist_name(market: str) -> str`
- 调整 `build_recommendation_scheduler_jobs`
- 调整 `sync_recommendations_to_default_watchlist`
- 推荐同步时 `purpose="system_research_pool"`

- [x] **步骤 4：运行测试验证通过**

运行：

`.venv\Scripts\python.exe -m pytest tests/test_data_sync_config.py::test_recommendation_jobs_enable_default_watchlist_intake tests/test_base_data_scheduler_analytics.py::test_recommendation_job_passes_watchlist_intake_options -q`

## 任务 4：研究跟踪入池规则和事件语义

**文件：**
- 修改：`src/finance_agent/agents/personal_assistant.py`
- 测试：`tests/test_base_data_scheduler_analytics.py`

- [x] **步骤 1：编写失败的测试**

测试应确认：

- `avoid` 和 `reject` 不入研究跟踪池。
- 入池项 `payload` 包含 `promotion_status="system_research"`、`expires_at` 和 `recommendation_run_id`。
- 事件类型为 `research_intake`，同时 payload 保留原推荐 ID。

- [x] **步骤 2：运行测试验证失败**

运行相关推荐入池测试。

- [x] **步骤 3：实现最少代码**

在推荐同步循环中：

- 跳过 `action in {"avoid", "reject"}`。
- `next_review_at = as_of + timedelta(days=3)`。
- `payload` 增加 `promotion_status` 和 `expires_at`。
- `event_type="research_intake"`。

- [x] **步骤 4：运行测试验证通过**

运行相关推荐入池测试和 `tests/test_base_data_scheduler_analytics.py` 中推荐同步相关测试。

## 任务 5：事件重点池解析器

**文件：**
- 创建：`src/finance_agent/application/event_priority_service.py`
- 修改：`scripts/data/collect_base_data.py`
- 测试：`tests/test_event_priority_service.py`
- 测试：`tests/test_base_data_scheduler_analytics.py`

- [x] **步骤 1：编写失败的测试**

测试应覆盖：

```python
def test_event_priority_resolver_prefers_user_watch_then_research_then_recommendations() -> None:
    ...
    assert [item.symbol for item in result] == ["600519", "000001", "600036"]
    assert result[0].sources == ("manual_watchlist",)
```

另一个测试确认创业板、科创板、北交所不会进入事件重点池。

- [x] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_event_priority_service.py -q`

预期：导入失败。

- [x] **步骤 3：实现解析器**

实现：

- `EventPriorityAsset` dataclass。
- `EventPriorityResolver.resolve_ashare_symbols(limit: int) -> list[str]`。
- 先取用户观察池：`purpose in {"manual_watchlist", "portfolio_watchlist", "fund_watchlist"}`。
- 再取系统研究跟踪池：`purpose="system_research_pool"`。
- 再取最近非 `avoid` / 非 `reject` 推荐。
- 全程使用资产准入服务过滤主板股票。

- [x] **步骤 4：替换新闻重点名单逻辑**

将 `resolve_ashare_priority_news_symbols` 改为优先调用 `EventPriorityResolver`，失败时再回退原逻辑。

- [x] **步骤 5：运行测试验证通过**

运行：

`.venv\Scripts\python.exe -m pytest tests/test_event_priority_service.py tests/test_stock_news_event_source.py tests/test_base_data_scheduler_analytics.py::test_ashare_event_refresh_runs_stock_news_for_priority_assets_in_batches -q`

## 任务 6：运行态配置和文档进度更新

**文件：**
- 修改：`runtime/base_data_scheduler/base_data_scheduler.json`
- 修改：`docs/superpowers/specs/2026-06-09-可交易资产池与观察链路重构设计.md`

- [x] **步骤 1：更新 runtime 配置**

将 `analytics.recommendations.ashare.all_a.params.watchlist_id` 改为 `watchlist:default-owner:ashare:research`。

- [x] **步骤 2：更新设计文档进度表**

将已完成任务状态从 `未开始` 更新为 `已完成` 或 `部分完成`，并注明剩余前端展示项。

- [x] **步骤 3：验证 JSON 可解析**

运行：

`.venv\Scripts\python.exe -c "import json; json.load(open('runtime/base_data_scheduler/base_data_scheduler.json', encoding='utf-8')); print('ok')"`

预期：输出 `ok`。

## 任务 7：最终回归验证

**文件：**
- 不新增文件。

- [x] **步骤 1：Python 编译检查**

运行：

`.venv\Scripts\python.exe -m py_compile src\finance_agent\application\asset_eligibility_service.py src\finance_agent\application\event_priority_service.py scripts\data\collect_base_data.py src\finance_agent\scheduler\base_data_scheduler.py src\finance_agent\data\sync_config.py src\finance_agent\agents\personal_assistant.py`

- [x] **步骤 2：目标测试**

运行：

`.venv\Scripts\python.exe -m pytest tests/test_asset_eligibility_service.py tests/test_event_priority_service.py tests/test_data_sync_config.py tests/test_base_data_scheduler_analytics.py::test_recommendation_job_passes_watchlist_intake_options tests/test_base_data_scheduler_analytics.py::test_ashare_event_refresh_runs_stock_news_for_priority_assets_in_batches tests/test_stock_news_event_source.py -q`

- [x] **步骤 3：相关数据层回归**

运行：

`.venv\Scripts\python.exe -m pytest tests/test_ashare_market_bars_lifecycle.py tests/test_data_sync_control_service.py tests/test_data_scheduler_progress.py -q`

- [x] **步骤 4：Git diff 检查**

运行：

`git diff --check`

说明：如果仅有既有 CRLF 提示，记录为 Windows 换行提示；如有空白错误则修复。

## 第二阶段：历史初始化边界与技术初筛池

**目标：** 在第一阶段资产准入、研究跟踪池和事件重点池已经完成的基础上，进一步拆清楚历史初始化任务边界，并新增历史行情完成后的技术初筛池。

**边界：**
- A 股主板个股使用 `ashare.bars.1d.bootstrap` 做 10 年日 K 初始化。
- ETF / LOF 使用基金历史日 K 初始化任务，不与 A 股主板个股混跑。
- 开放式基金使用 `fund.open.nav.bootstrap` 写入 `fund_nav_snapshots`，不写入股票 OHLCV 语义。
- 技术初筛池只做粗筛，不直接产生买入建议。

## 任务 8：修正历史初始化任务边界

**文件：**
- 修改：`docs/superpowers/specs/2026-06-09-可交易资产池与观察链路重构设计.md`
- 实现文件：`src/finance_agent/data/sync_config.py`
- 实现文件：`scripts/data/collect_base_data.py`
- 测试文件：`tests/test_data_sync_config.py`
- 测试文件：`tests/test_ashare_market_bars_lifecycle.py`

- [x] **步骤 1：修正文档中的初始化任务语义**

明确历史初始化不是单个混合任务，而是 A 股主板个股、ETF/LOF、开放式基金三类一次性任务。

- [x] **步骤 2：补充任务边界表**

在设计文档中列出 `ashare.bars.1d.bootstrap`、`fund.etf.bars.1d.bootstrap`、`fund.lof.bars.1d.bootstrap`、`fund.open.nav.bootstrap` 的资产范围、数据形态和结果表。

- [x] **步骤 3：补充任务配置回归测试**

运行：
`.venv\Scripts\python.exe -m pytest tests/test_data_sync_config.py -q`

预期新增断言：
- A 股 bootstrap 只描述主板股票。
- 基金 ETF/LOF/open fund bootstrap 独立存在。
- 开放式基金净值任务不落入 A 股 K 线任务。

- [x] **步骤 4：实现或校准任务配置**

如果测试暴露配置缺口，更新 `src/finance_agent/data/sync_config.py` 和 runtime 配置，确保前端任务列表能显示三类历史初始化任务。

## 任务 9：新增技术初筛池服务

**文件：**
- 创建：`src/finance_agent/application/technical_screening_service.py`
- 修改：`src/finance_agent/application/dashboard_service.py`
- 测试：`tests/test_technical_screening_service.py`
- 测试：`tests/test_dashboard_service.py`

- [x] **步骤 1：编写失败的技术初筛测试**

测试应覆盖：
- 只接收可交易资产池内资产。
- 最近 250 个交易日覆盖不足时跳过。
- 趋势、动量、波动、回撤、流动性规则能产生可解释的技术得分。
- 入池结果带有 `source_type="technical_screening"`、有效期和规则命中明细。

运行：
`.venv\Scripts\python.exe -m pytest tests/test_technical_screening_service.py -q`

预期：导入 `technical_screening_service` 失败。

- [x] **步骤 2：实现最小服务**

实现：
- `TechnicalScreeningService.screen_ashare(...)`
- `TechnicalScreeningService.screen_funds(...)`
- `TechnicalScreeningCandidate`
- 技术规则解释 payload

- [x] **步骤 3：接入池子表达**

短期优先写入 `screening_results` / `screening_result_items`。如需要在观察池页面展示，再同步到 `watchlists.purpose="technical_screening_pool"`。

- [x] **步骤 4：运行测试验证通过**

运行：
`.venv\Scripts\python.exe -m pytest tests/test_technical_screening_service.py tests/test_dashboard_service.py -q`

## 任务 10：新增技术初筛调度任务

**文件：**
- 修改：`src/finance_agent/data/sync_config.py`
- 修改：`src/finance_agent/scheduler/base_data_scheduler.py`
- 修改：`runtime/base_data_scheduler/base_data_scheduler.json`
- 测试：`tests/test_base_data_scheduler_analytics.py`
- 测试：`tests/test_data_sync_config.py`

- [x] **步骤 1：编写失败的调度配置测试**

新增断言：
- 存在 `analytics.technical_screening.ashare.main_board`。
- 可选存在 `analytics.technical_screening.fund.exchange_traded`。
- 技术初筛依赖历史行情或收盘行情任务成功。
- 技术初筛输出可被推荐任务或事件重点池读取。

- [x] **步骤 2：实现调度任务定义**

在 sync config 中增加技术初筛任务，建议先配置为 `after_success`，依赖 `ashare.bars.1d.close_final` 或历史初始化完成后的手工触发。

- [x] **步骤 3：实现调度器执行入口**

在 `BaseDataScheduler` 中增加调用入口，调用 `TechnicalScreeningService`，并把入池数量、跳过数量、规则命中分布写入任务 payload。

- [x] **步骤 4：运行调度回归**

运行：
`.venv\Scripts\python.exe -m pytest tests/test_base_data_scheduler_analytics.py tests/test_data_sync_config.py -q`

## 任务 11：把技术初筛接入后续链路

**文件：**
- 修改：`src/finance_agent/application/event_priority_service.py`
- 修改：`src/finance_agent/agents/personal_assistant.py`
- 修改：`src/finance_agent/application/dashboard_service.py`
- 测试：`tests/test_event_priority_service.py`
- 测试：`tests/test_base_data_scheduler_analytics.py`

- [x] **步骤 1：事件重点池读取技术初筛结果**

技术初筛池应低于用户观察池和系统研究跟踪池，高于普通最近推荐候选，用于驱动新闻和风险补抓。

- [x] **步骤 2：推荐流水线读取技术初筛结果**

推荐候选不再只从全资产池粗暴扫描，可优先读取技术初筛池，再补充其他高质量资产。

- [x] **步骤 3：前端展示技术初筛池**

Dashboard 增加技术初筛池分组或指标：入池数量、有效期、规则命中、最新运行时间。

- [x] **步骤 4：运行端到端回归**

运行：
`.venv\Scripts\python.exe -m pytest tests/test_event_priority_service.py tests/test_base_data_scheduler_analytics.py tests/test_dashboard_service.py -q`

## 第二阶段验证命令

完成第二阶段实现后至少运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_technical_screening_service.py tests/test_data_sync_config.py tests/test_base_data_scheduler_analytics.py tests/test_event_priority_service.py tests/test_dashboard_service.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd apps\agent-office
npm run build
cd ..\..
git diff --check
```
