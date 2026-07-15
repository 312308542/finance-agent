# A 股个人运行配置实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 在当前任务内逐项执行。步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 新增并默认启用 `personal-ashare` 数据同步预设，使日常调度只覆盖 A 股和基金，并让根 Compose 的 PostgreSQL/Redis 自动恢复。

**架构：** 复用现有 `build_preset_config()`、`preset_markets()` 和调度导出链路，不新增调度器实现。旧预设和历史别名保持兼容；根 Compose 只管理中间件，scheduler 继续使用独立模板。

**技术栈：** Python 3.12、pytest、argparse、Pydantic、Docker Compose、PyCharm Call Hierarchy。

---

## 文件结构

- 修改 `tests/test_data_sync_config.py`：锁定新预设、默认市场和调度计划无 crypto。
- 创建 `tests/test_data_sync_defaults.py`：锁定 CLI 与 API Schema 的默认预设。
- 修改 `tests/test_docker_deploy_templates.py`：锁定根 Compose 的 restart 策略和服务边界。
- 修改 `src/finance_agent/data/sync_config.py`：注册 `personal-ashare` 并迁移无参数默认值。
- 修改 `src/finance_agent/cli/data_sync.py`：迁移 CLI 默认值和帮助文本。
- 修改 `src/finance_agent/api/schemas.py`：迁移 API 请求模型默认预设和市场。
- 修改 `apps/agent-office/src/dataSyncView.ts`：注册前端预设市场并迁移未知预设兜底。
- 修改 `apps/agent-office/src/pages/DataSyncControlPanel.tsx`：迁移页面兜底预设并增加下拉选项。
- 修改 `apps/agent-office/scripts/test-data-sync-view.mjs`：锁定前端预设市场行为。
- 修改 `docker-compose.yml`：为 PostgreSQL、Redis 增加自动恢复策略。
- 修改 `docs/基础数据调度器.md`、`docs/运维手册.md`：同步预设和运行边界。
- 修改 `docs/项目稳定运行差距与决策清单.md`、`docs/项目进度跟踪表.md`：记录真实验证结果和进度。

### 任务 1：锁定数据同步默认行为

**文件：**
- 修改：`tests/test_data_sync_config.py`
- 创建：`tests/test_data_sync_defaults.py`

- [x] **步骤 1：对待编辑符号执行上游影响分析**

使用 PyCharm `analyze_calls(INCOMING_CALLS)` 或 IDE 引用分析检查 `build_preset_config`、`normalize_preset`、`preset_markets`、`preset_label`、`add_data_arguments`、`DataSyncConfigUpdateRequest`、`marketsForPreset` 和 `DataSyncControlPanel`。记录直接调用者、受影响入口和风险；若为 HIGH/CRITICAL，先向用户报告再继续。

> 2026-07-15 记录：PyCharm Python Call Hierarchy 未能解析模块级函数，改用 IDE `search_symbol` 与引用搜索。`build_preset_config()` 无参数默认值影响调度器默认计划/模板、配置中心无文件回退和配置读取服务，判定 HIGH，已在编辑前向负责人警告；其他符号为低到中风险。前端 `marketsForPreset` 只有配置页一个生产调用点，`DataSyncControlPanel` 被总览页和详情页复用，修改仅限预设兜底与下拉选项。

- [x] **步骤 2：编写失败测试**

```python
def test_personal_ashare_is_the_default_preset_without_crypto() -> None:
    config = build_preset_config()
    assert config.preset == "personal-ashare"
    assert list(config.markets) == ["ashare", "fund"]


def test_personal_comprehensive_remains_backward_compatible() -> None:
    config = build_preset_config("personal-comprehensive")
    assert list(config.markets) == [
        "ashare",
        "fund",
        "crypto_spot",
        "crypto_future",
    ]


def test_default_scheduler_plan_excludes_crypto_jobs() -> None:
    payload = export_scheduler_payload(build_preset_config())
    assert all(not job["name"].startswith("crypto") for job in payload["jobs"])


def test_cli_and_api_default_to_personal_ashare() -> None:
    args = build_parser().parse_args(["data", "config", "init"])
    assert args.preset == "personal-ashare"
    request = DataSyncConfigUpdateRequest()
    assert request.preset == "personal-ashare"
    assert request.markets == ["ashare", "fund"]
```

- [x] **步骤 3：运行红灯**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_data_sync_config.py tests\test_data_sync_defaults.py -q
```

预期：新增断言失败，现有默认仍为 `personal-comprehensive`，且 `personal-ashare` 尚未注册。

> 2026-07-15 红灯记录：Python 专项为 4 failed / 36 passed；前端 `marketsForPreset("unknown-preset")` 仍返回四市场并按预期失败；Compose 专项为 1 failed / 4 passed。失败原因均为新默认尚未实现。

### 任务 2：最小实现 `personal-ashare`

**文件：**
- 修改：`src/finance_agent/data/sync_config.py`
- 修改：`src/finance_agent/cli/data_sync.py`
- 修改：`src/finance_agent/api/schemas.py`
- 修改：`apps/agent-office/src/dataSyncView.ts`
- 修改：`apps/agent-office/src/pages/DataSyncControlPanel.tsx`
- 修改：`apps/agent-office/scripts/test-data-sync-view.mjs`

- [x] **步骤 1：注册预设并迁移默认值**

在 `DataSyncPreset`、`PRESETS` 和 `preset_label()` 中加入 `personal-ashare`；把 `build_preset_config()` 默认值改为 `personal-ashare`；让 `preset_markets("personal-ashare")` 返回 `ashare`、`fund`。历史别名映射保持不变。

- [x] **步骤 2：迁移 CLI 与 API 默认值**

CLI 未传 `--preset` 和 `DataSyncConfigUpdateRequest` 未传 `preset` 时使用 `personal-ashare`；API 默认市场改为 `ashare`、`fund`；帮助文本同时列出新预设。

- [x] **步骤 3：迁移前端兜底与预设选项**

在 `presetMarketDefaults` 中加入 `personal-ashare`，未知预设兜底改为 A 股与基金；配置页初始值、配置刷新兜底和预设下拉均加入 `personal-ashare`。先在 `test-data-sync-view.mjs` 写断言并确认失败，再做最小实现。

- [x] **步骤 4：运行绿灯和相邻回归**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_data_sync_config.py tests\test_data_sync_defaults.py tests\test_data_sync_control_service.py tests\test_scheduler_jobs_regenerable_from_preset.py tests\test_structural_scheduler_job.py tests\test_trigger_scheduler_jobs.py -q
node apps\agent-office\scripts\test-data-sync-view.mjs
```

预期：全部通过；显式 `personal-comprehensive` 测试无需批量改名。

> 2026-07-15 记录：Python 相邻回归 79 passed；前端数据同步视图脚本退出 0；PyCharm 项目构建返回成功。

### 任务 3：中间件自动恢复

**文件：**
- 修改：`tests/test_docker_deploy_templates.py`
- 修改：`docker-compose.yml`

- [x] **步骤 1：编写失败测试**

```python
def test_root_compose_restarts_only_postgres_and_redis() -> None:
    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert content.count("restart: unless-stopped") == 2
    assert "finance-agent-scheduler" not in content
```

- [x] **步骤 2：运行红灯**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_docker_deploy_templates.py -q
```

预期：因根 Compose 尚无 restart 策略而失败。

- [x] **步骤 3：最小修改 Compose**

只给 `postgres` 和 `redis` 增加 `restart: unless-stopped`，不合并 scheduler 模板，不改变端口、数据卷、健康检查和密码配置。

- [x] **步骤 4：验证模板和解析结果**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_docker_deploy_templates.py -q
docker compose config
docker inspect finance-agent-timescaledb --format '{{.HostConfig.RestartPolicy.Name}}'
docker inspect finance-agent-redis --format '{{.HostConfig.RestartPolicy.Name}}'
```

先用 `docker compose up -d postgres redis` 把新策略应用到现有容器，再执行 inspect；预期两项均为 `unless-stopped`，容器健康检查继续为 healthy。

> 2026-07-15 记录：模板 5 passed，`docker compose config` 解析成功；真实重建后 PostgreSQL/Redis 的 restart 均为 `unless-stopped`、health 均为 healthy，`GET /api/health` 返回 API/数据库 `ok`。

### 任务 4：真实默认计划验收

**文件：**
- 运行时证据：`runtime/base_data_scheduler/personal-ashare.json`，不提交。

- [x] **步骤 1：导出默认配置**

```powershell
.\.venv\Scripts\python.exe -m finance_agent.cli data config init --output runtime\base_data_scheduler\personal-ashare.json
```

预期输出 `preset=personal-ashare`，市场只有 `ashare`、`fund`。

- [x] **步骤 2：打印调度计划并 dry-run**

```powershell
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\personal-ashare.json --print-plan
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base_data_scheduler\personal-ashare.json --run-once --dry-run
```

预期：任务名称中没有 `crypto`；只生成 A 股、基金及其分析/维护任务；命令不请求外部数据源。

> 2026-07-15 记录：导出配置为 `personal-ashare`，市场为 `ashare,fund`；计划 37 项、启用 33 项，其中 A 股前缀 16 项、基金前缀 6 项、crypto 任务 0；dry-run 状态文件为 `completed/run_once`、37 项/启用 33 项、`last_error=null`。

### 任务 5：文档、回归与变更审计

**文件：**
- 修改：`docs/基础数据调度器.md`
- 修改：`docs/运维手册.md`
- 修改：`docs/项目稳定运行差距与决策清单.md`
- 修改：`docs/项目进度跟踪表.md`

- [x] **步骤 1：同步中文文档**

记录 `personal-ashare` 为新默认、crypto 必须显式启用、旧预设兼容、根 Compose 只自动恢复 PostgreSQL/Redis、API/调度器仍由用户手工启动。清单中 RUN-003 只有在真实 dry-run 通过后才标为 `100%`；RUN-001/RUN-002 继续暂缓，不能借中间件恢复策略宣称完成。

- [x] **步骤 2：运行全量验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix apps\agent-office run build
git diff --check
```

预期：后端测试全绿，前端生产构建成功，差异无空白错误。

> 2026-07-15 记录：全量后端 707 passed；前端 `tsc -b && vite build` 成功，1,722 个模块完成构建；`git diff --check` 通过。

- [x] **步骤 3：执行提交前变更审计**

使用 PyCharm Git diff、调用分析和 Git 原生命令检查实际改动只覆盖默认预设、Compose 恢复策略、测试和对应文档；本轮按负责人授权不调用 WSL GitNexus。确认 `.ai/`、`.codex/`、`artifacts/`、`runtime/` 和密钥均未暂存。

> 2026-07-15 记录：PyCharm 错误级检查 8 个代码/测试文件均为 0；代码批次暂存 10 个文件，禁止目录暂存数为 0，cached diff check 通过。

- [x] **步骤 4：提交**

按代码测试批次和文档收口批次提交，提交信息使用中文 Conventional Commits；提交后重新检查工作区，只保留用户已有的非提交目录。

> 2026-07-15 记录：代码、测试和 Compose 已提交为 `58a1df6`（`feat(调度): 默认使用A股与基金配置`）；本计划、设计规格、运维和进度文档作为独立文档批次提交。
