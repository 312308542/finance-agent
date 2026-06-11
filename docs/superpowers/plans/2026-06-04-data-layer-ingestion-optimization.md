# 数据层采集优化实现计划

> **面向 AI 代理的工作者：** 必须逐项执行并更新 `docs/数据层采集优化进度表.md`。每个代码阶段遵循测试先行，避免把已有未提交的前端、模型配置或无关文档改动混入本次提交。

**目标：** 将新闻和 K 线采集从“全市场逐票高频扫”改为“水位增量、失败延迟重试、重点优先、按数据源限流”的可恢复链路。

**架构：** 新增持久化采集水位，调度时通过水位选择真正需要采集的资产；失败记录进入延迟重试，不阻塞当前批次；新闻使用全市场事件源优先，逐票新闻仅覆盖重点资产；按数据源 host 做限流和退避。

**技术栈：** Python、SQLAlchemy、PostgreSQL、Redis 进度缓存、pytest、现有 `collect_base_data.py` 调度入口。

---

### 任务 1：进度表和计划落盘

**文件：**
- 创建：`docs/数据层采集优化进度表.md`
- 创建：`docs/superpowers/plans/2026-06-04-data-layer-ingestion-optimization.md`

- [x] 步骤 1：创建中文进度表和实施计划
- [x] 步骤 2：更新 DLO-001 状态为完成

### 任务 2：采集水位持久层

**文件：**
- 修改：`src/finance_agent/storage/orm.py`
- 修改：`src/finance_agent/storage/repositories.py`
- 创建：`src/finance_agent/storage/migrations/versions/20260604_0013_add_data_sync_watermarks.py`
- 测试：`tests/test_data_sync_watermarks.py`

- [x] 步骤 1：编写失败测试，验证水位可记录成功、失败次数和下次重试时间
- [x] 步骤 2：实现 `data_sync_watermarks` ORM 和 Repository
- [x] 步骤 3：运行水位测试并通过

### 任务 3：A 股 K 线水位增量

**文件：**
- 修改：`scripts/data/collect_base_data.py`
- 测试：`tests/test_base_data_scheduler_analytics.py`

- [x] 步骤 1：编写失败测试，验证 K 线只选择缺失、过期或到期重试标的
- [x] 步骤 2：在 K 线批处理成功后更新成功水位
- [x] 步骤 3：在网络失败后写入失败水位和 `next_retry_at`
- [x] 步骤 4：运行相关调度测试并通过

### 任务 4：新闻链路改造

**文件：**
- 修改：`scripts/data/collect_base_data.py`
- 修改：`src/finance_agent/data/collectors.py`
- 修改：`src/finance_agent/data/providers/akshare_p1_provider.py`
- 测试：`tests/test_stock_news_event_source.py`
- 测试：`tests/test_base_data_scheduler_analytics.py`

- [x] 步骤 1：编写失败测试，验证逐票新闻只覆盖重点资产集合
- [x] 步骤 2：保留全市场事件/公告源为主链路
- [x] 步骤 3：将新闻原文抓取失败记录为补充失败，不阻塞事件入库

### 任务 5：数据源限流和退避

**文件：**
- 创建：`src/finance_agent/data/source_rate_limiter.py`
- 修改：`scripts/data/collect_base_data.py`
- 测试：`tests/test_source_rate_limiter.py`

- [x] 步骤 1：编写失败测试，验证同一 host 的并发和最小间隔限制
- [x] 步骤 2：实现轻量本地限流器
- [x] 步骤 3：接入 K 线和逐票新闻采集

### 任务 6：调度配置、文档和验证

**文件：**
- 修改：`runtime/base_data_scheduler/base_data_scheduler.json`
- 修改：`docs/基础数据调度器.md`
- 修改：`docs/数据库设计.md`
- 修改：`docs/AKShare能力矩阵.md`

- [x] 步骤 1：将盘中高频任务限定为实时行情和重点新闻
- [x] 步骤 2：说明水位表、失败重试、限流和新闻冷热路径
- [x] 步骤 3：运行相关 pytest 和静态检查
- [x] 步骤 4：更新进度表 DLO-010 状态
