import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Buffer } from "node:buffer";
import ts from "typescript";

const source = await readFile(new URL("../src/dataSyncView.ts", import.meta.url), "utf8");
const panelSource = await readFile(
  new URL("../src/pages/DataSyncControlPanel.tsx", import.meta.url),
  "utf8",
);
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const {
  summarizeSchedulerStatus,
  pickEnabledMarkets,
  marketsForPreset,
  filterPreviewTasksByMarkets,
  processingStatusLabel,
  schedulerStartFeedback,
  summarizeSchedulerWritePolicy,
  summarizeProcessingPlan,
  summarizeSchedulerRuntime,
  schedulerRuntimeTaskDetail,
  filterSchedulerRuntimeTasks,
} = await import(moduleUrl);

assert.deepEqual(
  pickEnabledMarkets({
    markets: {
      ashare: { enabled: true },
      crypto_spot: { enabled: false },
      crypto_future: { enabled: true },
    },
  }),
  ["ashare", "crypto_future"],
);

const runtimeSummary = summarizeSchedulerRuntime({
  runtime: {
    source: "postgresql",
    status_counts: {
      scheduled: 1,
      blocked: 2,
      pending: 3,
      running: 1,
      completed: 8,
      failed: 1,
      cancelled: 1,
    },
    listed_status_counts: {
      scheduled: 1,
      blocked: 1,
      pending: 1,
      running: 0,
      completed: 0,
      failed: 0,
      cancelled: 0,
    },
    tasks: [
      { task_id: "task:pending", job_name: "pending.job", status: "pending" },
      {
        task_id: "task:blocked",
        job_name: "blocked.job",
        status: "blocked",
        blocked_reason: "recovery_data_domain",
        blocked_until: "2026-08-31T09:00:00+00:00",
        blocked_detail: {
          domains: ["ashare_quotes"],
          blocking_task_ids: ["task:upstream"],
          mutex_key: "quotes",
          resource_pool: "realtime",
          running: 2,
          limit: 2,
          blockers: [{ run_id: "run:1", step_id: "step:quotes", target_id: "target:600519" }],
        },
      },
    ],
    config_drift: true,
    scheduler_config_digest: "scheduler-digest",
    api_config_digest: "api-digest",
  },
});

assert.equal(runtimeSummary.source, "postgresql");
assert.equal(runtimeSummary.statusCounts.pending, 3);
assert.equal(runtimeSummary.statusCounts.blocked, 2);
assert.equal(runtimeSummary.listedStatusCounts.blocked, 1);
assert.equal(runtimeSummary.listedStatusCounts.completed, 0);
assert.equal(runtimeSummary.configDrift, true);
assert.deepEqual(
  filterSchedulerRuntimeTasks(runtimeSummary.tasks, "blocked").map((task) => task.job_name),
  ["blocked.job"],
);
assert.match(panelSource, /调度运行快照/);
assert.match(panelSource, /配置摘要不一致/);
assert.match(panelSource, /计划中/);
assert.match(panelSource, /已阻塞/);
assert.match(panelSource, /待执行/);
assert.doesNotMatch(panelSource, /health\?\.status \?\? "missing"/);
assert.match(panelSource, /（累计）/);
assert.match(panelSource, /listedStatusCounts/);

const blockedDetail = schedulerRuntimeTaskDetail(runtimeSummary.tasks[1]);
assert.match(blockedDetail, /ashare_quotes/);
assert.match(blockedDetail, /task:upstream/);
assert.match(blockedDetail, /quotes/);
assert.match(blockedDetail, /realtime 2 \/ 2/);
assert.match(blockedDetail, /run:1/);
assert.match(blockedDetail, /step:quotes/);
assert.match(blockedDetail, /target:600519/);
assert.match(blockedDetail, /2026-08-31T09:00:00\+00:00/);

assert.deepEqual(marketsForPreset("ashare-comprehensive"), ["ashare"]);
assert.deepEqual(marketsForPreset("crypto-comprehensive"), ["crypto_spot", "crypto_future"]);
assert.deepEqual(marketsForPreset("personal-ashare"), ["ashare", "fund"]);
assert.deepEqual(marketsForPreset("unknown-preset"), ["ashare", "fund"]);
assert.match(panelSource, /<option value="personal-ashare">personal-ashare<\/option>/);

assert.deepEqual(
  filterPreviewTasksByMarkets(
    [
      { task_key: "ashare.bars.1d", market: "ashare" },
      { task_key: "fund.nav", market: "fund" },
      { task_key: "crypto_spot.bars.1h", market: "crypto_spot" },
      { task_key: "quality.global" },
    ],
    ["ashare"],
  ).map((task) => task.task_key),
  ["ashare.bars.1d", "quality.global"],
);

assert.deepEqual(
  summarizeSchedulerStatus({
    status: "degraded",
    health: {
      status: "missing",
      healthy: false,
      database_status: "error",
      database_error: "database unavailable",
    },
    process: { managed_by: "docker-compose", service: "finance-agent-scheduler", running: false },
  }),
  {
    tone: "red",
    label: "状态降级",
    detail: "Docker finance-agent-scheduler · PostgreSQL 状态不可用，调度器存活未知",
  },
);

assert.deepEqual(
  summarizeSchedulerStatus({
    status: "healthy",
    health: {
      state: "running",
      last_job: "crypto_spot.bars.1h",
      last_job_status: "executed",
    },
    process: { running: true, pid: 1234 },
  }),
  {
    tone: "green",
    label: "运行中",
    detail: "PID 1234 · crypto_spot.bars.1h / executed",
  },
);

assert.deepEqual(
  summarizeSchedulerStatus({
    managed_by: "docker-compose",
    service: "finance-agent-scheduler",
    health: {
      state: "running",
      last_job: "ashare.realtime_quotes",
      last_job_status: "executed",
    },
    process: { managed_by: "docker-compose", service: "finance-agent-scheduler", running: true },
  }),
  {
    tone: "green",
    label: "运行中",
    detail: "Docker finance-agent-scheduler · ashare.realtime_quotes / executed",
  },
);

assert.equal(processingStatusLabel("requires_universe_selection"), "需选择候选池");

assert.deepEqual(
  schedulerStartFeedback(true, {
    status: "ok",
    message: "已启动 1 轮预演调度：只生成执行计划，不会写入数据库。",
    data: { writes_enabled: false },
  }),
  {
    statusText: "已启动 1 轮预演调度：只生成执行计划，不会写入数据库。",
    modeLabel: "预演模式",
    writePolicy: "不入库",
  },
);

assert.deepEqual(
  schedulerStartFeedback(false, {
    status: "ok",
    message: "已启动真实数据同步：会调用采集器并写入数据库。",
    data: { writes_enabled: true },
  }),
  {
    statusText: "已启动真实数据同步：会调用采集器并写入数据库。",
    modeLabel: "真实同步",
    writePolicy: "会入库",
  },
);

assert.deepEqual(
  schedulerStartFeedback(false, {
    status: "ok",
    message: "Docker 调度器当前运行中；Windows API 不执行启动操作，请使用 docker compose 管理服务。",
    data: { managed_by: "docker-compose", service: "finance-agent-scheduler", running: true },
  }),
  {
    statusText: "Docker 调度器当前运行中；Windows API 不执行启动操作，请使用 docker compose 管理服务。",
    modeLabel: "Docker 托管",
    writePolicy: "容器执行",
  },
);

assert.deepEqual(
  summarizeSchedulerWritePolicy({
    health: { status: "healthy", state: "running" },
    process: { running: true, dry_run: false },
  }),
  {
    statusText: "真实数据同步运行中：采集结果会写入数据库",
    modeLabel: "真实同步",
    writePolicy: "会入库",
  },
);

assert.deepEqual(
  summarizeSchedulerWritePolicy({
    health: { status: "healthy", payload: { dry_run: true, state: "completed" } },
    process: { running: false },
  }),
  {
    statusText: "最近一次为预演调度：只生成计划，未写入数据库",
    modeLabel: "预演模式",
    writePolicy: "不入库",
  },
);

assert.deepEqual(
  summarizeProcessingPlan({
    processing: {
      normalization: { status: "active_with_collection" },
      analytics: {
        scheduler_status: "requires_universe_selection",
        required_runtime_input: "universe_id",
      },
      stages: [{ stage_key: "normalization" }, { stage_key: "analytics.indicators" }],
    },
  }),
  {
    normalizationLabel: "随采集执行",
    analyticsLabel: "需选择候选池",
    stageCount: 2,
    requiredInput: "universe_id",
  },
);
