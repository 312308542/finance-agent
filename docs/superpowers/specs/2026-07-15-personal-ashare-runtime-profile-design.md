# A 股个人运行配置设计

> 日期：2026-07-15
> 决策：项目负责人已确认采用方案 A。
> 范围：RUN-003 与根 Compose 中间件恢复策略。

## 1. 目标

为日常个人运行提供一个默认只覆盖 A 股和基金的数据同步预设，避免基础数据调度器在未显式选择数字货币配置时启动 crypto 任务；同时让根 `docker-compose.yml` 中的 PostgreSQL 和 Redis 在 Docker Desktop/daemon 恢复后自动拉起。

本设计不改变调度器的进程模型。项目运行期间仍由用户手工启动 API 和基础数据调度器，不注册 Windows 计划任务，也不默认启动 scheduler 容器。

## 2. 预设行为

新增预设 `personal-ashare`，中文名称为“私人助手 A 股与基金模式”。其默认市场固定为：

```text
ashare
fund
```

以下既有预设继续保留：

| 预设 | 默认市场 | 兼容策略 |
| --- | --- | --- |
| `personal-ashare` | A 股、基金 | 新默认 |
| `personal-comprehensive` | A 股、基金、crypto 现货、crypto 合约 | 完整保留，显式选择时行为不变 |
| `ashare-comprehensive` | A 股 | 完整保留 |
| `crypto-comprehensive` | crypto 现货、crypto 合约 | 独立保留，默认不启用 |
| `lightweight` | A 股、基金、crypto 现货 | 完整保留，显式选择时行为不变 |

历史别名 `personal` 继续映射到 `personal-comprehensive`，避免旧脚本静默改变市场范围。新默认只通过无参数调用、CLI 默认值和 API 请求模型默认值体现，不迁移或覆盖用户已经保存的配置文件。

## 3. 默认入口

以下四类入口统一改为 `personal-ashare`：

1. `build_preset_config()` 无参数调用。
2. `finance-agent data config init` 未传 `--preset` 时的默认值。
3. `DataSyncConfigUpdateRequest` 未传 `preset` 时的默认值，其默认 `markets` 同步改为 `ashare`、`fund`。
4. Web 配置页缺少后端配置时的兜底预设、预设市场映射和下拉选项。

`load_data_sync_config(path)` 读取已有 JSON 时继续尊重文件中的 `preset` 和 `markets`，不做隐式迁移。调度器默认计划由 `build_preset_config()` 生成，因此自动继承 A 股与基金范围。

## 4. 调度边界

`personal-ashare` 导出的调度计划必须满足：

- 包含 A 股采集、A 股分析/推荐与基金任务。
- 不包含 `crypto_spot`、`crypto_future` 市场配置。
- 不包含任何 crypto 采集、分析或推荐任务。
- `crypto-comprehensive` 仍能单独导出数字货币任务。
- 现有 manual/bootstrap 任务的启用状态保持不变，本次不借机调整任务频率、并发、评分或推荐逻辑。

## 5. Docker 恢复策略

根 `docker-compose.yml` 的 `postgres` 和 `redis` 服务增加：

```yaml
restart: unless-stopped
```

该策略只保证 Docker daemon 可用时容器异常退出或主机重启后的自动恢复。用户显式停止容器后，Docker 不会强制重新拉起。`deploy/docker/compose.scheduler.yml` 保持独立，不合并到根 Compose，也不随 PostgreSQL/Redis 默认启动。

## 6. 安全边界

本设计不把 API 监听地址改为 `0.0.0.0`。当前 API 没有认证、TLS 和访问审计，`SEC-001` 已由负责人明确暂缓，因此继续绑定 `127.0.0.1`；局域网暴露不能被描述为已安全完成。

本设计也不注册 `FinanceAgent-Api` 或 `FinanceAgent-BaseDataScheduler`，不执行整机重启验收。

## 7. 测试与真实验收

按 TDD 完成以下验证：

1. 单元测试锁定新默认预设、市场范围、中文名称和旧预设兼容性。
2. CLI 解析测试锁定未传 `--preset` 时使用 `personal-ashare`。
3. API Schema 测试锁定默认请求值和默认市场。
4. 前端数据同步视图测试锁定新预设、未知预设兜底市场和配置页下拉选项。
5. 调度计划测试锁定默认计划没有 crypto 任务，显式 crypto 预设仍可用。
6. Compose 模板测试锁定 PostgreSQL、Redis 均为 `restart: unless-stopped`，scheduler 不在根 Compose。
7. `docker compose config` 验证 YAML 合法且两项 restart 策略生效。
8. 导出默认配置并执行调度器 `--print-plan`/dry-run，确认只出现获批市场与任务，不发起真实采集。
9. 运行相关回归、全量 pytest、前端生产构建和 `git diff --check`。

## 8. 回滚

若新默认造成兼容问题，只需把三个默认入口恢复为 `personal-comprehensive`；新增预设继续保留，不影响显式使用者。若 Docker 恢复策略不符合本机运维习惯，可删除根 Compose 的两行 `restart`，数据卷和现有容器数据不会被修改。
