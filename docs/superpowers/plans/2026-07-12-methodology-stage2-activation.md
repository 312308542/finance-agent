# 方案 23 阶段二方法论能力激活实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 完成方案 23 阶段二 T6/T7：受预算约束地启用组合经理/风险反驳纯知识技能，并把 Ichimoku 确定性计算接入既有收盘后结构任务、指标帧和 capability 门控。

**架构：** 默认技能注册表继续以 `load_active_methodology_skills()` 为唯一入口，候选集扩展为 P1、已启用纯知识 L2 和 capability-gated 技能；Prompt 的 1200/6000 字符预算与 P1→L1→L2 优先级保持不变。Ichimoku 复用 `analytics.structural.ashare.daily` 的 K 线读取与指标帧入库，不新建调度任务；`ichimoku_v1` 成功入库后静态 capability 才翻为可用。相关性、配对交易和季节性继续保持不可用，并在技能文档明确为接口预留。

**技术栈：** Python 3.12、pytest、SQLAlchemy Repository、pandas、现有 `BaseDataScheduler`、GitNexus 1.6.8（WSL CLI fallback）。

---

## 文件结构

- 修改 `src/finance_agent/skills/loader.py`：声明并加载阶段二纯知识技能；把 Ichimoku 纳入 capability-gated 候选集并在 T7 翻位。
- 修改 `src/finance_agent/application/structural_methodology_service.py`：在同一批 K 线中计算/持久化 `ichimoku_v1`，并为预热不足生成可审计空帧。
- 修改 `src/finance_agent/data/sync_config.py`：默认结构任务显式包含 `ichimoku`。
- 修改 `src/finance_agent/scheduler/base_data_scheduler.py`：无显式 engines 时的默认值包含 `ichimoku`。
- 修改 `src/finance_agent/skills/{correlation-analysis,pair-trading,seasonal}/SKILL.md`：声明引擎接口预留和消费场景前置条件。
- 修改 `tests/test_skill_loader.py`：锁定 T6/T7 默认激活集合和不可用引擎门控。
- 修改 `tests/test_roundtable_skill_injection.py`：锁定组合经理/风险反驳注入与总预算。
- 修改 `tests/test_structural_scheduler_job.py`：锁定调度导出、成功帧、预热不足帧与幂等入库。
- 修改 `docs/开发方案/23-方法论能力接入与技能激活策略.md`、`20-金融分析方法论技能库.md`、`00-总体规划与执行约定.md`：同步阶段二真实进度和验证证据。

### 任务 1：T6 启用 L2 纯知识技能

**文件：**
- 修改：`tests/test_skill_loader.py`
- 修改：`tests/test_roundtable_skill_injection.py`
- 修改：`src/finance_agent/skills/loader.py`

- [x] **步骤 1：编写失败的 loader 测试**

在 `tests/test_skill_loader.py` 增加：

```python
def test_load_active_skills_enable_stage2_pure_knowledge_roles() -> None:
    registry = load_active_methodology_skills()

    assert {skill.name for skill in registry.for_role("portfolio_manager")} >= {
        "asset-allocation",
        "hedging-strategy",
        "etf-analysis",
        "fund-analysis",
        "convertible-bond",
        "cross-market-strategy",
    }
    assert {skill.name for skill in registry.for_role("risk_rebuttal")} >= {
        "credit-analysis",
        "geopolitical-risk",
    }
```

- [x] **步骤 2：编写失败的 Prompt 预算测试**

在 `tests/test_roundtable_skill_injection.py` 增加参数化测试，分别检查 `portfolio_manager`、`risk_rebuttal`：目标技能可见、技能区块不超过 6000 字符、角色红线仍存在。

- [x] **步骤 3：运行红灯**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_skill_loader.py tests\test_roundtable_skill_injection.py -q
```

预期：新增测试失败，组合经理/风险反驳尚未加载上述 L2 技能。

- [x] **步骤 4：实现最小 L2 候选集**

在 `loader.py` 声明：

```python
L2_PURE_KNOWLEDGE_SKILL_NAMES: tuple[str, ...] = (
    "asset-allocation",
    "hedging-strategy",
    "etf-analysis",
    "fund-analysis",
    "convertible-bond",
    "cross-market-strategy",
    "credit-analysis",
    "geopolitical-risk",
)
```

将其追加到 `load_active_methodology_skills()` 的候选 names；继续经过 `is_skill_capability_available()`，不增加绕过门控的特殊分支。

- [x] **步骤 5：运行绿灯与相邻回归**

运行同一 pytest 命令，预期全部通过；再运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_p2_methodology_skills.py tests\test_p3_methodology_skills.py -q
```

- [x] **步骤 6：GitNexus 检测并提交 T6**

暂存上述三份文件，运行 WSL GitNexus 1.6.8：

```bash
gitnexus detect-changes --repo finance-agent --scope staged
```

确认只影响技能加载与圆桌 Prompt 后提交：

```text
feat(技能): 分批启用L2纯知识方法论
```

### 任务 2：T7 接入 Ichimoku 调度与指标帧

**文件：**
- 修改：`tests/test_structural_scheduler_job.py`
- 修改：`src/finance_agent/application/structural_methodology_service.py`
- 修改：`src/finance_agent/data/sync_config.py`
- 修改：`src/finance_agent/scheduler/base_data_scheduler.py`

- [x] **步骤 1：完成待修改符号 impact**

依次对 `StructuralMethodologyRefreshService.refresh`、`compute_engine_payloads`、`normalize_engines`、`build_structural_methodology_scheduler_jobs`、`BaseDataScheduler.build_structural_methodology_refresh_kwargs` 做 upstream impact；HIGH/CRITICAL 立即停止并报告。

- [x] **步骤 2：编写失败的调度契约测试**

把默认导出断言改为：

```python
assert job["params"]["engines"] == [
    "swings", "smc", "harmonic", "elliott", "ichimoku"
]
```

并新增默认 kwargs 测试，确认未显式传 engines 时也包含 `ichimoku`。

- [x] **步骤 3：编写失败的成功入库测试**

用 60 根确定性 K 线运行 `StructuralMethodologyRefreshService.refresh(engines=["ichimoku"])`，断言：

```python
assert result["engine_counts"] == {"ichimoku": 1}
assert saved[0]["horizon"] == "ichimoku_v1"
assert saved[0]["payload"]["status"] == "available"
assert saved[0]["payload"]["lines"]
assert saved[0]["payload"]["evidence_id"].startswith("ichimoku:")
```

- [x] **步骤 4：编写失败的预热不足测试**

用 3 根 K 线运行同一服务，断言仍写入一个 `ichimoku_v1` 帧，状态为 `insufficient_data`、`error_count == 0`，且 payload 保留 evidence_id、中文 caveat 和不得由 LLM 补算的红线。

- [x] **步骤 5：运行红灯**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_structural_scheduler_job.py -q
```

预期：默认 engines 不含 Ichimoku、服务拒绝 `ichimoku` 或没有写入目标帧。

- [x] **步骤 6：实现最小计算与空帧路径**

在服务中：

```python
DEFAULT_STRUCTURAL_ENGINES = ("swings", "smc", "harmonic", "elliott", "ichimoku")
ENGINE_SCHEMA_BY_NAME["ichimoku"] = "ichimoku_v1"
```

`compute_engine_payloads()` 对至少 52 根 K 线调用 `IchimokuAdapter.compute(...).to_indicator_payload()`；不足 52 根时调用新的 `build_ichimoku_insufficient_payload()`，不得让单标的进入 errors，也不得影响前四个结构引擎。

- [x] **步骤 7：同步两个默认配置入口**

`build_structural_methodology_scheduler_jobs()` 和 `build_structural_methodology_refresh_kwargs()` 的默认 engines 同步增加 `ichimoku`；不新建任务、不改变依赖、优先级、资源池或 limit。

- [x] **步骤 8：运行绿灯与结构回归**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_structural_scheduler_job.py tests\test_data_sync_config.py tests\test_run_base_data_scheduler_cli.py -q
```

预期全部通过。

### 任务 3：T7 capability 翻位与孤儿引擎落档

**文件：**
- 修改：`tests/test_skill_loader.py`
- 修改：`tests/test_roundtable_skill_injection.py`
- 修改：`src/finance_agent/skills/loader.py`
- 修改：`src/finance_agent/skills/correlation-analysis/SKILL.md`
- 修改：`src/finance_agent/skills/pair-trading/SKILL.md`
- 修改：`src/finance_agent/skills/seasonal/SKILL.md`

- [x] **步骤 1：编写失败的 Ichimoku 激活测试**

断言 `ENGINE_CAPABILITIES["ichimoku"].available is True`、默认 registry 和技术分析师 Prompt 均出现 `ichimoku`，同时 `chanlun-interpret`、`correlation-analysis`、`pair-trading`、`seasonal` 仍不出现。

- [x] **步骤 2：运行红灯**

运行技能加载与 Prompt 测试，预期 Ichimoku 断言失败。

- [x] **步骤 3：翻位并纳入 gated 候选集**

把 `ichimoku` 加入现有引擎门控扩展候选集，并将 capability 描述改为已接入 `analytics.structural.ashare.daily`、产出 `ichimoku_v1`。不把三个接口预留引擎翻为可用。

- [x] **步骤 4：补孤儿引擎文档状态**

三份 SKILL.md 在输入前增加“接入状态”小节：确定性适配器已实现，但生产调度/消费场景未建立，capability 保持 False，禁止默认加载。

- [x] **步骤 5：运行绿灯**

运行技能、Prompt、P2/P3 防回归测试，预期全部通过。

- [x] **步骤 6：GitNexus 检测并提交 T7**

暂存任务 2/3 的代码、测试和技能文档，运行 staged detect-changes，确认影响符合结构调度、指标帧和圆桌技能激活范围后提交：

```text
feat(方法论): 接入Ichimoku调度与能力激活
```

### 任务 4：真实验收与文档同步

**文件：**
- 修改：`docs/开发方案/23-方法论能力接入与技能激活策略.md`
- 修改：`docs/开发方案/20-金融分析方法论技能库.md`
- 修改：`docs/开发方案/00-总体规划与执行约定.md`

- [x] **步骤 1：运行方案 23 专项测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_skill_loader.py tests\test_roundtable_skill_injection.py tests\test_p2_methodology_adapters.py tests\test_p2_methodology_skills.py tests\test_p3_methodology_skills.py tests\test_structural_scheduler_job.py tests\test_data_sync_config.py tests\test_run_base_data_scheduler_cli.py -q
```

- [x] **步骤 2：运行真实结构调度**

```powershell
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --config runtime\base-data-scheduler.personal-comprehensive.json --run-once --only analytics.structural.ashare.daily
```

若本地导出配置不存在，先用项目既有 `data config export` 入口重新生成，禁止手写运行态配置。验收必须确认 `engine_counts.ichimoku > 0`、`error_count == 0`，并只读查询最新 `indicator_frames.horizon='ichimoku_v1'`。

- [x] **步骤 3：运行全量验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix apps\agent-office run build
```

- [x] **步骤 4：同步文档**

只写入实际命令输出和真实计数；T6/T7 标记为已完成，阶段三外部条件不提前解禁。

- [x] **步骤 5：文档提交前检测并提交**

暂存三份文档，运行 staged detect-changes，提交：

```text
docs(方案): 完成方法论阶段二验收
```

### 任务 5：阶段二收尾审计

- [x] **步骤 1：检查工作区与提交边界**

确认没有 `.ai/`、`.codex/`、`artifacts/`、`runtime/` 或本地密钥进入暂存/提交。

- [x] **步骤 2：比较当前分支差异**

运行：

```bash
gitnexus detect-changes --repo finance-agent --scope compare --base-ref main
```

若输出因历史 100+ 提交过大而截断，以本阶段三个 staged 检测为准，并明确记录限制。

- [x] **步骤 3：重新读取方案 23 进度表**

逐项确认 T6/T7、验证记录、阶段三边界和 capability 状态相互一致，再进入下一子项目。

> 收尾记录（2026-07-12）：三次 staged detect-changes 均完整返回，T6/T7 代码最终为 low risk、0 个受影响执行流程；相对 `main` 的 compare 因包含 100+ 历史提交，在 180 秒内无输出并受控超时，因此不作为本阶段风险结论。暂存边界仅包含计划/方案文档，本地 `.ai/`、`.codex/`、`artifacts/` 未进入提交。
