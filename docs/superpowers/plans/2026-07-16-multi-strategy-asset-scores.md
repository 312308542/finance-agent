# 多策略评分持久化实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 为 `asset_scores` 增加正式策略维度，使短线和题材评分可并存、可分别推荐和回测。

**架构：** 在评分主表增加非空 `strategy_id`，评分 ID 追加策略摘要；生产流水线显式透传策略，推荐和回测按物理列过滤。迁移回填历史 payload 并同步更新推荐引用。

**技术栈：** Python 3.12、SQLAlchemy 2、PostgreSQL、Alembic、pytest、PyCharm References/Call Hierarchy。

---

## 文件结构

- 创建 `tests/test_asset_score_strategy_dimension.py`：锁定 ORM、ID、仓储筛选和迁移文本契约。
- 修改 `tests/test_scoring_strategies.py`：锁定评分服务写入物理策略 ID。
- 修改 `tests/test_recommendation_pipeline_guard.py`：锁定流水线向推荐透传策略。
- 修改 `tests/test_backtest_runner.py`：锁定回测使用物理策略列且不回退。
- 创建 `src/finance_agent/storage/migrations/versions/20260716_0021_add_asset_score_strategy.py`：生产迁移与安全降级。
- 修改 `src/finance_agent/storage/orm.py`：新增 `AssetScoreORM.strategy_id` 和索引。
- 修改 `src/finance_agent/storage/repositories.py`：写入及查询策略过滤。
- 修改 `src/finance_agent/scoring/service.py`：有效策略 ID 与策略化评分 ID。
- 修改 `src/finance_agent/pipelines/recommendation.py`：向推荐层透传评分策略。
- 修改 `src/finance_agent/recommendations/service.py`：按策略读取评分并保存审计字段。
- 修改 `src/finance_agent/backtesting/runner.py`：按物理列严格筛选策略。
- 修改方案 18、待决策记录、项目清单和数据库文档：同步真实验收结果。

### 任务 1：评分策略维度红灯

- [x] 在 `tests/test_asset_score_strategy_dimension.py` 编写失败测试，断言 `AssetScoreORM.strategy_id` 存在、`build_score_id()` 对不同策略返回不同 ID、迁移包含历史回填/推荐引用/非空约束。
- [x] 在 `tests/test_scoring_strategies.py` 增加断言：`upsert_asset_score` 收到 `strategy_id`，生成 ID 带稳定策略摘要。
- [x] 运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_asset_score_strategy_dimension.py tests\test_scoring_strategies.py -q
```

预期：因 ORM 列、迁移文件和新参数不存在而失败。

### 任务 2：最小数据模型与评分写入

- [x] 创建 0021 迁移：增加可空列、回填策略与 payload、同步重写推荐/评分 ID、改非空、建立索引；downgrade 先检查冲突。
- [x] 修改 ORM 和仓储，让 `upsert_asset_score()` 必须接收并写入 `strategy_id`。
- [x] 修改 `build_score_id(..., strategy_id)`，用 `md5(strategy_id)[:12]` 追加策略摘要。
- [x] 修改 `ScoringService`，显式策略使用配置 ID；无策略时写 `strategy:{market}:legacy_default`。
- [x] 运行任务 1 测试，预期全部通过。

### 任务 3：推荐与回测严格隔离红灯

- [x] 在 `tests/test_recommendation_pipeline_guard.py` 断言推荐调用收到 `score_strategy_id`。
- [x] 在 `tests/test_backtest_runner.py` 断言编译 SQL 包含 `asset_scores.strategy_id = ...`，并删除 payload 匹配失败后回退其他评分的行为。
- [x] 运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_recommendation_pipeline_guard.py tests\test_backtest_runner.py -q
```

预期：推荐未透传、回测 SQL 未过滤物理列而失败。

### 任务 4：最小查询链实现

- [x] `list_scores_for_screening(screening_id, strategy_id=None)` 和 `get_latest_score(..., strategy_id=None)` 增加可选过滤。
- [x] `RecommendationService.rank_from_screening(..., score_strategy_id=None)` 按策略读取并在 run/source payload 保存 ID。
- [x] `UniverseRecommendationPipeline` 把 `strategy_id` 作为 `score_strategy_id` 传给推荐层。
- [x] `DatabaseBacktestScoreSource` 在 SQL 中强制策略列过滤，移除 payload 后过滤和 fallback。
- [x] 运行任务 3 测试及推荐、触发、回测相邻回归，预期全部通过。

### 任务 5：迁移与真实双策略验收

- [x] 迁移前记录评分行数、缺策略数、历史推荐非空引用数及悬空数。
- [x] 执行 `alembic upgrade head`，确认 head 为 `20260716_0021`。
- [x] 验证行数不变、`strategy_id` 无空值、历史推荐引用悬空数为 0。
- [x] 对最新真实 screening 运行 `short_swing` 与 `theme_momentum` 评分，确认两套各 2,900 条、ID 不重叠，短线数值未被题材写入覆盖。
- [x] 分别运行两套 `factor_score_topn` 回测；不存在策略必须返回 partial/空评分，不得回退。

### 任务 6：回归、文档与提交

- [x] 运行专项评分/推荐/回测/触发测试。
- [x] 运行 `pytest -q`、前端生产构建、`git diff --check`。
- [x] 使用 PyCharm References 与 Git 暂存区 diff 审计受影响符号；确认 `.ai/.codex/artifacts/runtime` 未暂存。
- [x] 同步中文文档和 D-016/SCORE-001 进度，只按真实结果更新百分比。
- [x] 提交：`feat(评分): 支持多策略评分并存`。
