# 多策略评分持久化设计

> 日期：2026-07-16
> 决策：负责人批准 D-016 方案 A。
> 风险：HIGH，涉及评分主表、推荐引用、回测筛选和生产历史数据迁移。

## 1. 目标

让同一 `screening_id`、同一资产和同一因子帧可以同时保存短线、题材等多套评分，彼此不覆盖，并让推荐与回测显式消费指定策略。改造必须保持现有短线推荐行为、历史推荐引用和触发链兼容。

## 2. 数据模型

`asset_scores` 新增非空列：

```text
strategy_id varchar(128) not null
```

评分 ID 由原有基础 ID 加策略摘要组成：

```text
score:{universe_id}:{asset_id}:{horizon}:{factor_frame_id}:strategy:{md5(strategy_id)[:12]}
```

使用摘要是为了避免长策略名突破现有 ID 长度。`asset_scores.score_id` 与 `asset_recommendations.score_id` 长度同步扩为 255。

新增索引：

- `idx_asset_scores_screening_strategy_rank(screening_id, strategy_id, rank)`
- `idx_asset_scores_asset_strategy_horizon_asof(asset_id, strategy_id, horizon, as_of)`

不增加外键：历史评分必须允许引用已经归档或不再存在于 `scoring_strategies` 的策略标识。

## 3. 历史数据迁移

当前生产库有 13,569 条评分：9,828 条 payload 含 `strategy:ashare:short_swing`，3,741 条没有策略标识。

迁移规则：

1. 优先取 `payload.strategy_id`。
2. 其次取 `payload.score_strategy_id`。
3. A 股缺失值写为 `strategy:ashare:legacy_default`；其他市场使用 `strategy:{market}:legacy_default`。
4. 把最终策略写回 payload 的 `strategy_id`，保证旧审计工具继续可读。
5. 先更新 `asset_recommendations.score_id`，再更新 `asset_scores.score_id`，保持历史推荐引用一致。
6. 最后把 `strategy_id` 改为非空并建立索引。

降级前若移除策略摘要后出现重复基础 ID，迁移必须拒绝降级，避免静默丢评分；无冲突时同步还原推荐引用和评分 ID。

## 4. 写入与查询

`ScoringService` 总是计算有效策略 ID：显式策略使用其 ID；未传策略时使用 `strategy:{market}:legacy_default`。该 ID 同时写入物理列、payload 和评分 ID。

`AssetScoreRepository`：

- 写入必须提供 `strategy_id`。
- `list_scores_for_screening()` 支持可选策略过滤。
- `get_latest_score()` 支持可选策略过滤，未传时保留既有兼容行为。

`UniverseRecommendationPipeline` 把调度参数 `strategy_id` 同时传给评分和推荐。`RecommendationService` 只读取该策略的评分，确保同一 screening 下不会混排两套分数。

`DatabaseBacktestScoreSource` 直接用 `asset_scores.strategy_id` 过滤；请求的策略没有评分时返回空结果，不再回退到其他策略。

## 5. 安全边界

- 不改变评分公式、策略权重、买入分位线或推荐动作规则。
- 不自动启用题材策略调度；只建立并存能力。
- 不修改历史分数数值，只补策略维度和重写稳定 ID。
- 迁移前后评分行数必须相同，历史推荐的非空 `score_id` 必须全部能关联评分表。
- 迁移使用事务，失败时整体回滚。

## 6. 验收

1. 同一资产、因子帧使用短线和题材策略生成两个不同 `score_id`。
2. 同一 screening 可同时查询两套各自独立的排序。
3. 推荐只消费显式指定策略。
4. 回测请求不存在的策略时不得借用其他策略评分。
5. 迁移后 13,569 条评分仍全部保留，历史推荐引用无悬空。
6. 在真实 screening 上并存写入题材评分后，短线评分数量和数值不变。
7. 专项、全量 pytest、前端构建和 readiness 均通过。

## 7. 回滚

代码回滚前先执行 Alembic downgrade。若库中已经存在同一基础 ID 的多策略评分，降级会明确拒绝；需要先归档非主策略评分，再执行降级。该限制是防止数据丢失的刻意设计。

## 8. 实施与真实验收

2026-07-16 已完成 D-016 方案 A：

- Alembic `20260716_0021` 已应用到真实 PostgreSQL；迁移前后 13,569 条评分数量一致，`strategy_id` 空值、payload 不一致、ID 摘要不一致和历史推荐悬空引用均为 0。
- 历史 2,201 条非空推荐评分引用全部同步为策略化 ID；`asset_scores.score_id` 和 `asset_recommendations.score_id` 已扩为 255。
- 同一真实 screening 已保存短线和题材各 2,900 条评分，合计 5,800 个唯一 ID；两套分数全部不同，2,894 个名次不同，短线数值摘要保持不变，Top20 重合 7/20。
- 短线与题材推荐 run 各生成 20 条，40 条推荐的物理评分策略错配数为 0。
- 回测 SQL 已修复“限流早于每资产最新评分选择”的问题；两套回测选股与各自物理 Top20 差异均为 0，不存在策略返回 `partial`、0 个标的和“评分截面为空”。
- 全量后端 `754 passed in 28.80s`，前端 TypeScript/Vite 生产构建通过，API/数据库健康为 `ok`，调度器迁移后已重启并继续更新心跳。

一年 `replayed` 回测只用于验证策略隔离和数据链路，不构成题材权重有效性证据；SCORE-001 继续等待无前视的连续截面或历史因子重算。
