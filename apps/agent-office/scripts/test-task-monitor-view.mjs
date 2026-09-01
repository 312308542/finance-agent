import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/taskMonitorView.ts", import.meta.url), "utf8");
const styleSource = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const appSource = await readFile(new URL("../src/pages/TaskMonitorPage.tsx", import.meta.url), "utf8");
const apiSource = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
const mainSource = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");
const dataSyncPanelSource = await readFile(new URL("../src/pages/DataSyncControlPanel.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const {
  buildTaskMonitorModel,
  describeSchedulerJob,
  estimateRemainingSeconds,
  extractSchedulerJobTasks,
  filterTaskMonitorItems,
  formatDuration,
  formatTaskLogLine,
  startSerialPolling,
  statusTone,
  taskProgressLabel,
} = await import(moduleUrl);

const progress = buildTaskMonitorModel({
  status: "ok",
  data: {
    cache_backend: "redis",
    generated_at: "2026-06-03T15:35:13+08:00",
    tasks: [
      {
        job_name: "ashare.bars.1d",
        run_id: "ashare.bars.1d:20260603T153012:8f3a",
        title: "补采 A 股 1d K 线",
        status: "running",
        started_at: "2026-06-03T15:30:12+08:00",
        updated_at: "2026-06-03T15:35:12+08:00",
        summary: {
          total_items: 5236,
          completed_items: 3524,
          running_items: 4,
          failed_items: 2,
          retry_items: 1,
          remaining_items: 1710,
          progress_ratio: 0.673,
        },
        stages: [
          {
            stage_key: "sync_market_bars",
            title: "同步 K 线",
            status: "running",
            total_items: 5236,
            completed_items: 3524,
            failed_items: 2,
            progress_ratio: 0.673,
            updated_at: "2026-06-03T15:35:12+08:00",
          },
        ],
        recent_events: [
          {
            event_type: "symbol_completed",
            symbol: "600519",
            name: "贵州茅台",
            status: "completed",
            batch_index: 18,
            batch_count: 27,
            created_at: "2026-06-03T15:35:12+08:00",
          },
          {
            event_type: "symbol_failed",
            symbol: "301611",
            status: "failed",
            batch_index: 18,
            batch_count: 27,
            retry_after_seconds: 900,
            next_retry_at: "2026-06-03T15:50:12+08:00",
            provider_key: "akshare:stock_zh_a_hist_tx",
            error_message: "curl: (56) Connection closed abruptly",
            created_at: "2026-06-03T15:35:14+08:00",
          },
        ],
        metrics: {
          duration_seconds: 300,
          max_workers: 4,
          throughput_per_minute: 704.8,
          node: "local",
          cache_backend: "redis",
        },
      },
    ],
    waiting: [
      {
        job_name: "crypto_future.bars.1h",
        title: "crypto_future.bars.1h",
        status: "waiting",
        interval_seconds: 300,
      },
      {
        job_name: "quality.ashare",
        title: "quality.ashare",
        status: "waiting",
        interval_seconds: 300,
      },
      {
        job_name: "analytics.recommendations.crypto_spot.binance",
        title: "analytics.recommendations.crypto_spot.binance",
        status: "waiting",
        interval_seconds: 300,
      },
    ],
    source_rate_states: [
      {
        source_key: "stock_zh_a_hist_tx",
        success_count: 420,
        failure_count: 18,
        timeout_count: 9,
        disconnect_count: 6,
        rate_limited_count: 0,
        failure_rate: 0.12,
        effective_max_concurrency: 1,
        effective_min_interval_seconds: 2,
        next_recover_at: "2026-06-03T15:50:12+08:00",
        updated_at: "2026-06-03T15:35:14+08:00",
      },
      {
        source_key: "eastmoney_kline_cookie",
        state: "cooling",
        cooldown_remaining_seconds: 600,
        last_error_message: "curl: (56) Connection closed abruptly",
        failure_rate: 1,
        effective_max_concurrency: 0,
        updated_at: "2026-06-03T15:35:15+08:00",
      },
    ],
    global_concurrency: {
      running: 2,
      limit: 4,
    },
    resource_pools: {
      collection_heavy: {
        running: 1,
        queued: 2,
        limit: 2,
      },
      realtime: {
        running: 1,
        queued: 0,
        limit: 1,
      },
    },
    metrics: {
      running_count: 1,
      waiting_count: 1,
      failed_count: 0,
      completed_recent_count: 0,
    },
  },
});

assert.equal(progress.connectionStatus, "ok");
assert.equal(progress.cacheBackend, "redis");
assert.equal(progress.items.length, 4);
assert.deepEqual(
  progress.items.map((item) => [
    item.jobName,
    item.title,
    item.status,
    item.statusLabel,
    item.description,
    item.progressRatio,
    item.isRealtime,
  ]),
  [
    [
      "ashare.bars.1d",
      "补采 A 股 1d K 线",
      "running",
      "运行中",
      "按股票批次增量同步前复权日线行情，用于技术指标和推荐计算。",
      0.673,
      true,
    ],
    [
      "crypto_future.bars.1h",
      "Binance 合约 1 小时 K 线",
      "waiting",
      "等待中",
      "按交易对批次同步合约小时线行情，供趋势和波动因子使用。",
      0,
      false,
    ],
    [
      "quality.ashare",
      "A 股数据质量检查",
      "waiting",
      "等待中",
      "检查 A 股资产、行情、基本面、资金流和事件数据覆盖率，识别缺口与过期数据。",
      0,
      false,
    ],
    [
      "analytics.recommendations.crypto_spot.binance",
      "Binance 现货推荐计算",
      "waiting",
      "等待中",
      "基于现货行情、趋势因子和风险信号生成数字货币现货候选推荐。",
      0,
      false,
    ],
  ],
);
assert.equal(progress.items[0].stages[0].completedItems, 3524);
assert.equal(progress.items[0].summary.retryItems, 1);
assert.equal(progress.items[0].maxWorkers, 4);
assert.equal(progress.items[0].throughputPerMinute, 704.8);
assert.equal(progress.items[0].events.length, 2);
assert.equal(progress.items[0].events[1].nextRetryAt, "2026-06-03T15:50:12+08:00");
assert.equal(progress.items[0].events[1].providerKey, "akshare:stock_zh_a_hist_tx");

const persistentInstances = buildTaskMonitorModel({
  status: "ok",
  data: {
    tasks: [
      {
        task_id: "task:instance-1",
        job_name: "ashare.events",
        status: "running",
        progress: {
          run_id: "ashare.events:run-1",
          title: "刷新 A 股新闻和公告",
          summary: {
            total_items: 200,
            completed_items: 66,
            running_items: 134,
            failed_items: 0,
            remaining_items: 134,
            progress_ratio: 0.33,
          },
        },
      },
      {
        task_id: "task:instance-2",
        job_name: "ashare.events",
        status: "running",
        progress: {
          run_id: "ashare.events:run-2",
          title: "刷新 A 股新闻和公告",
          summary: {
            total_items: 200,
            completed_items: 80,
            running_items: 120,
            failed_items: 0,
            remaining_items: 120,
            progress_ratio: 0.4,
          },
        },
      },
    ],
    waiting: [],
  },
});
assert.deepEqual(
  persistentInstances.items.map((item) => [item.id, item.runId, item.progressRatio]),
  [
    ["task:instance-1", "ashare.events:run-1", 0.33],
    ["task:instance-2", "ashare.events:run-2", 0.4],
  ],
);
assert.equal(persistentInstances.items[0].summary.completedItems, 66);
assert.equal(persistentInstances.items[0].progressAvailable, true);
assert.equal(taskProgressLabel(persistentInstances.items[0]), "33%");

const atomicRunningTask = buildTaskMonitorModel({
  status: "ok",
  data: {
    tasks: [
      {
        task_id: "task:atomic",
        job_name: "analytics.universe.rebuild_avoid_pool.ashare",
        status: "running",
      },
    ],
    waiting: [],
  },
});
assert.equal(atomicRunningTask.items[0].progressAvailable, false);
assert.equal(taskProgressLabel(atomicRunningTask.items[0]), "执行中");

const persistentStates = buildTaskMonitorModel({
  status: "ok",
  data: {
    tasks: [
      { task_id: "task:scheduled", job_name: "scheduled.job", status: "scheduled" },
      { task_id: "task:blocked", job_name: "blocked.job", status: "blocked" },
      { task_id: "task:pending", job_name: "pending.job", status: "pending" },
      { task_id: "task:cancelled", job_name: "cancelled.job", status: "cancelled" },
    ],
  },
});
assert.deepEqual(
  persistentStates.items.map((item) => [item.status, item.statusLabel]),
  [
    ["waiting", "等待中"],
    ["blocked", "已阻塞"],
    ["waiting", "等待中"],
    ["cancelled", "已取消"],
  ],
);
assert.deepEqual(progress.sourceRateStates, [
  {
    sourceKey: "stock_zh_a_hist_tx",
    successCount: 420,
    failureCount: 18,
    timeoutCount: 9,
    disconnectCount: 6,
    rateLimitedCount: 0,
    failureRate: 0.12,
    effectiveMaxConcurrency: 1,
    effectiveMinIntervalSeconds: 2,
    nextRecoverAt: "2026-06-03T15:50:12+08:00",
    updatedAt: "2026-06-03T15:35:14+08:00",
    state: "",
    cooldownRemainingSeconds: 0,
    lastErrorMessage: "",
    probeOk: false,
  },
  {
    sourceKey: "eastmoney_kline_cookie",
    successCount: 0,
    failureCount: 0,
    timeoutCount: 0,
    disconnectCount: 0,
    rateLimitedCount: 0,
    failureRate: 1,
    effectiveMaxConcurrency: 0,
    effectiveMinIntervalSeconds: 0,
    nextRecoverAt: "",
    updatedAt: "2026-06-03T15:35:15+08:00",
    state: "cooling",
    cooldownRemainingSeconds: 600,
    lastErrorMessage: "curl: (56) Connection closed abruptly",
    probeOk: false,
  },
]);
assert.match(appSource, /东方财富 K 线 Cookie/);
assert.match(appSource, /Cookie 冷却中/);
assert.equal(progress.metrics.runningCount, 1);
assert.deepEqual(progress.globalConcurrency, { running: 2, limit: 4 });
assert.deepEqual(progress.resourcePools.collection_heavy, {
  name: "collection_heavy",
  running: 1,
  queued: 2,
  limit: 2,
});
assert.equal(statusTone("running"), "blue");
assert.equal(statusTone("completed"), "green");
assert.equal(statusTone("failed"), "red");
const schedulerCatalogTasks = extractSchedulerJobTasks({
  data: {
    jobs: [
      {
        name: "ashare.bars.1d.bootstrap",
        enabled: false,
        schedule_type: "manual",
        interval_seconds: 0,
        priority: 900,
        resource_pool: "collection_heavy",
      },
      {
        name: "disabled.interval.job",
        enabled: false,
        schedule_type: "interval",
        interval_seconds: 3600,
      },
    ],
  },
});
assert.deepEqual(
  schedulerCatalogTasks.map((item) => item.job_name),
  ["ashare.bars.1d.bootstrap"],
);
assert.deepEqual(
  schedulerCatalogTasks.map((item) => [item.job_name, item.priority, item.resource_pool]),
  [["ashare.bars.1d.bootstrap", 900, "collection_heavy"]],
);
const progressWithCatalog = buildTaskMonitorModel(
  {
    status: "ok",
    data: {
      cache_backend: "redis",
      tasks: [{ job_name: "ashare.universe.all", status: "running" }],
      waiting: [],
    },
  },
  { fallbackWaiting: schedulerCatalogTasks },
);
assert.deepEqual(
  progressWithCatalog.items.map((item) => [item.jobName, item.title, item.status]),
  [
    ["ashare.universe.all", "A 股资产池刷新", "running"],
    ["ashare.bars.1d.bootstrap", "A 股 10 年历史日 K 初始化", "waiting"],
  ],
);
const completeButStillRunning = buildTaskMonitorModel({
  status: "ok",
  data: {
    cache_backend: "redis",
    tasks: [
      {
        job_name: "ashare.universe.all",
        title: "A 股资产池刷新",
        status: "running",
        summary: {
          total_items: 1,
          completed_items: 1,
          running_items: 0,
          failed_items: 0,
          remaining_items: 0,
          progress_ratio: 1,
        },
        stages: [
          {
            stage_key: "ashare_p0_assets",
            title: "ashare_p0_assets",
            status: "completed",
            total_items: 1,
            completed_items: 1,
            failed_items: 0,
            progress_ratio: 1,
          },
        ],
      },
    ],
  },
});
assert.equal(completeButStillRunning.items[0].status, "running");
assert.equal(completeButStillRunning.items[0].tone, "blue");
assert.equal(completeButStillRunning.items[0].progressRatio, 1);
assert.match(
  styleSource,
  /\.task-card-meta\s*{[\s\S]*?display:\s*grid;[\s\S]*?grid-template-columns:\s*minmax\(54px, auto\) minmax\(0, 1fr\);/,
);
assert.match(styleSource, /\.task-list-progress\s*{[\s\S]*?grid-template-columns:\s*1fr;/);
assert.doesNotMatch(styleSource, /\.task-list-progress\s*{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) 42px;/);
assert.match(styleSource, /\.task-list-item\s*{[\s\S]*?grid-template-rows:\s*auto auto;/);
assert.match(styleSource, /\.task-card-meta\s*{[\s\S]*?grid-column:\s*2;/);
assert.match(styleSource, /\.task-card-meta small\s*{[\s\S]*?min-width:\s*0;[\s\S]*?max-width:\s*100%;/);
assert.match(styleSource, /\.task-card-meta small:last-child\s*{[\s\S]*?grid-column:\s*1 \/ -1;/);
assert.match(
  styleSource,
  /\.task-card-icon\s*{[\s\S]*?display:\s*inline-flex;[\s\S]*?align-items:\s*center;[\s\S]*?justify-content:\s*center;[\s\S]*?overflow:\s*hidden;[\s\S]*?white-space:\s*normal;/,
);
assert.doesNotMatch(styleSource, /\.task-list-item\s*>\s*span/);
assert.match(
  appSource,
  /<TaskProgressBar[\s\S]*?ratio={task\.progressRatio}[\s\S]*?tone={task\.tone}[\s\S]*?indeterminate={!task\.progressAvailable && task\.status === "running"}[\s\S]*?<\/div>\s*<\/div>\s*<div className="task-card-actions">[\s\S]*?<div className="task-card-meta">/,
);
assert.match(appSource, /label="待重试"[\s\S]*?task\.summary\.retryItems/);
assert.match(appSource, /const \[logFilter, setLogFilter\]/);
assert.equal(
  appSource.includes('<button className="task-log-filter" type="button" disabled>只看异常</button>'),
  false,
);
assert.match(appSource, /filteredEvents\.map/);
assert.match(styleSource, /\.task-metric-card\.tone-red\s*>\s*span/);
assert.match(
  styleSource,
  /\.task-metric-grid\s*{[\s\S]*?grid-template-columns:\s*repeat\(auto-fit, minmax\(150px, 1fr\)\);/,
);
const taskLogSpanRules = [...styleSource.matchAll(/\.task-log-list span\s*{([^}]*)}/g)].map(
  (match) => match[1],
);
const activeTaskLogSpanRule = taskLogSpanRules.at(-1) ?? "";
assert.match(activeTaskLogSpanRule, /display:\s*block;/);
assert.match(activeTaskLogSpanRule, /overflow:\s*hidden;/);
assert.match(activeTaskLogSpanRule, /white-space:\s*nowrap;/);
assert.match(activeTaskLogSpanRule, /text-overflow:\s*ellipsis;/);
assert.match(appSource, /const \[logDetail, setLogDetail\]/);
assert.match(appSource, /className="task-log-popover"/);
assert.match(appSource, /<pre>{logDetail\.content}<\/pre>/);
assert.match(appSource, /title={logLine}/);
assert.match(appSource, /sourceRateStates = selectSourceRateStates\(task, model\.sourceRateStates\)/);
assert.match(appSource, /effectiveSourceConcurrency/);
assert.match(appSource, /label="全局任务并发"/);
assert.match(appSource, /label="资源池占用"/);
assert.match(appSource, /label="任务 worker"/);
assert.match(appSource, /label="任务优先级"/);
assert.match(appSource, /label="源有效并发"/);
assert.match(appSource, /数据源限频与退避/);
assert.match(appSource, /source-rate-list/);
assert.match(appSource, /立即执行/);
assert.match(appSource, /预演一次/);
assert.match(appSource, /编辑配置/);
assert.match(appSource, /任务配置/);
assert.match(appSource, /保存配置/);
assert.match(appSource, /保存并执行/);
assert.match(appSource, /className="task-card-actions"/);
assert.match(appSource, /runDataSchedulerJob\(task\.jobName/);
assert.match(appSource, /rerunFailedDataSchedulerJob\(task\.jobName/);
assert.match(appSource, /重跑失败项/);
assert.match(appSource, /task\.status === "running"/);
assert.match(appSource, /task\.summary\.failedItems <= 0/);
assert.match(appSource, /openConfigSignal/);
assert.match(appSource, /className="task-card-hover-text task-card-description"/);
assert.match(appSource, /data-tooltip=\{task\.description\}/);
assert.match(appSource, /data-tooltip=\{`任务 ID：\$\{task\.jobName \|\| "未命名 job"\}`\}/);
assert.match(
  styleSource,
  /\.task-card-hover-text:where\(:hover, :focus-visible\)::after\s*{[\s\S]*?content:\s*attr\(data-tooltip\);[\s\S]*?white-space:\s*normal;/,
);
assert.match(apiSource, /loadDataSchedulerJobs/);
assert.match(apiSource, /runDataSchedulerJob/);
assert.match(apiSource, /rerunFailedDataSchedulerJob/);
assert.match(apiSource, /\/rerun-failed/);
assert.match(apiSource, /updateDataSchedulerJob/);
assert.doesNotMatch(mainSource, /setInterval\s*\(/);
assert.match(mainSource, /startSerialPolling\s*\(/);
assert.match(apiSource, /TASK_MONITOR_REQUEST_TIMEOUT_MS/);
assert.match(apiSource, /taskMonitorRequestErrorMessage/);
assert.match(appSource, /taskProgressLabel\(task\)/);
assert.match(appSource, /indeterminate={!task\.progressAvailable/);
assert.match(
  appSource,
  /waiting:\s*model\.items\.filter\(\(item\) => \["waiting", "blocked", "locked"\]\.includes\(item\.status\)\)\.length/,
);
assert.match(apiSource, /resource_pools\?: Record<string, DataSyncResourcePoolPayload>/);
assert.match(dataSyncPanelSource, /资源池配置/);
assert.match(dataSyncPanelSource, /resourcePools/);
assert.match(dataSyncPanelSource, /setResourcePoolLimit/);
assert.match(dataSyncPanelSource, /resource_pools: resourcePools/);
assert.match(styleSource, /\.task-log-popover\s*{[\s\S]*?position:\s*absolute;/);
assert.match(styleSource, /\.task-log-popover pre\s*{[\s\S]*?white-space:\s*pre-wrap;/);
assert.match(styleSource, /\.task-config-drawer\s*{/);
assert.match(styleSource, /\.task-config-grid\s*{/);
assert.match(
  styleSource,
  /\.source-rate-list\s*{[\s\S]*?grid-template-columns:\s*repeat\(auto-fit, minmax\(260px, 1fr\)\);/,
);
assert.match(
  styleSource,
  /\.source-rate-item\s*{[\s\S]*?grid-template-columns:\s*38px minmax\(0, 1fr\);/,
);
assert.match(
  styleSource,
  /\.source-rate-metrics span,\s*\.source-rate-footer span\s*{[\s\S]*?overflow:\s*hidden;[\s\S]*?text-overflow:\s*ellipsis;/,
);

assert.deepEqual(
  filterTaskMonitorItems(progress.items, "running", "").map((item) => item.jobName),
  ["ashare.bars.1d"],
);
assert.deepEqual(
  filterTaskMonitorItems(progress.items, "all", "bars").map((item) => item.jobName),
  ["ashare.bars.1d", "crypto_future.bars.1h"],
);
assert.deepEqual(
  filterTaskMonitorItems(progress.items, "all", "market_bars").map((item) => item.jobName),
  ["ashare.bars.1d", "crypto_future.bars.1h"],
);
assert.deepEqual(
  filterTaskMonitorItems(progress.items, "waiting", "ashare").map((item) => item.jobName),
  ["quality.ashare"],
);

assert.equal(
  formatTaskLogLine(progress.items[0].events[0]),
  "[15:35:12] 完成 600519 贵州茅台 · 批次 18/27",
);
assert.equal(
  formatTaskLogLine({
    ...progress.items[0].events[0],
    status: "skipped",
  }),
  "[15:35:12] 跳过 600519 贵州茅台 · 批次 18/27",
);
assert.equal(
  formatTaskLogLine(progress.items[0].events[1]),
  "[15:35:14] 失败 301611 · 批次 18/27 · 下次重试 2026-06-03 15:50 · akshare:stock_zh_a_hist_tx · curl: (56) Connection closed abruptly",
);
assert.equal(formatDuration(300), "5m 0s");
assert.equal(formatDuration(3725), "1h 2m");
assert.equal(estimateRemainingSeconds(42, 0, 0, 0), 42);
assert.equal(estimateRemainingSeconds(null, 10, 5, 120), 60);
assert.equal(estimateRemainingSeconds(null, 0, 5, 120), null);
assert.deepEqual(describeSchedulerJob("ashare.bars.1d.bootstrap"), {
  title: "A 股 10 年历史日 K 初始化",
  description: "手动执行的一次性初始化任务，补齐最近 10 年 A 股前复权日 K；不会自动进入周期调度。",
});
assert.deepEqual(describeSchedulerJob("ashare.bars.1d.midday_partial"), {
  title: "A 股午盘临时日 K 同步",
  description: "交易日午盘后同步当天临时日 K，写入 partial 状态；用于午盘后的数据校验和观察。",
});
assert.deepEqual(describeSchedulerJob("ashare.bars.1d.close_final"), {
  title: "A 股收盘最终日 K 同步",
  description: "交易日收盘后同步当天最终日 K，写入 available 状态；用于当日指标、风险和推荐计算。",
});
assert.deepEqual(describeSchedulerJob("ashare.bars.1d.revision"), {
  title: "A 股凌晨日 K 修正",
  description: "凌晨低频修正近 7 日 K 线、复权变化和漏采记录，优先补齐失败或过期标的。",
});
assert.deepEqual(describeSchedulerJob("ashare.events"), {
  title: "A 股新闻公告同步",
  description: "同步新闻、公告和事件线索，后续用于事件清洗、衰减和 Agent 新闻检索。",
});
assert.deepEqual(describeSchedulerJob("ashare.news_articles"), {
  title: "A 股新闻正文补抓",
  description: "低并发补抓已入库新闻的原文正文，回填事件和证据 payload，不阻塞新闻列表同步。",
});
assert.doesNotMatch(appSource, /单次上限/);
assert.doesNotMatch(appSource, /读取新的单次上限/);
assert.match(appSource, /K 线条数上限/);
assert.match(appSource, /10 年历史日 K 按日期范围拉取，不受条数上限截断/);
assert.deepEqual(describeSchedulerJob("quality.crypto_future"), {
  title: "合约数据质量检查",
  description: "检查 Binance 合约资产、小时线和衍生品指标覆盖率，识别缺口与异常数据。",
});
assert.deepEqual(describeSchedulerJob("analytics.recommendations.ashare.all_a"), {
  title: "A 股全市场推荐计算",
  description: "基于最新行情、因子、风险和数据质量生成 A 股候选池评分与推荐结果。",
});
assert.deepEqual(describeSchedulerJob("analytics.technical_screening.ashare.main_board"), {
  title: "A 股主板技术初筛",
  description: "基于最新日 K 计算技术指标并写入 indicator_frames，为主板股票生成技术初筛结果。",
});
assert.deepEqual(describeSchedulerJob("analytics.universe.merge.ashare.recommendation"), {
  title: "A 股推荐候选池合并",
  description: "合并技术初筛、研究池和回避池规则，生成推荐流水线使用的 A 股候选 Universe。",
});
assert.deepEqual(describeSchedulerJob("analytics.triggers.evaluate.daily"), {
  title: "每日触发事件评估",
  description: "读取推荐、风险、观察池和数据质量变化，生成需要 Agent 消费的触发事件。",
});
assert.equal(
  describeSchedulerJob("custom.future.task").description,
  "调度器任务，正在等待执行或写入实时进度。",
);

const degraded = buildTaskMonitorModel(
  {
    status: "degraded",
    message: "Redis 不可用，无法读取实时进度。",
    data: {
      cache_backend: "null",
      tasks: [],
      waiting: [],
    },
  },
  {
    fallbackWaiting: [
      {
        job_name: "ashare.events",
        title: "同步 A 股事件",
        interval_seconds: 300,
      },
    ],
  },
);

assert.equal(degraded.connectionStatus, "degraded");
assert.equal(degraded.message, "Redis 不可用，无法读取实时进度。");
assert.deepEqual(
  degraded.items.map((item) => [item.jobName, item.status, item.isRealtime]),
  [["ashare.events", "waiting", false]],
);

let firstPollResolve;
let secondPollResolve;
const firstPoll = new Promise((resolve) => {
  firstPollResolve = resolve;
});
const secondPoll = new Promise((resolve) => {
  secondPollResolve = resolve;
});
const scheduledPolls = [];
const clearedPolls = [];
let pollCount = 0;
const stopPolling = startSerialPolling(
  () => {
    pollCount += 1;
    return pollCount === 1 ? firstPoll : secondPoll;
  },
  2000,
  {
    setTimeout(callback, delay) {
      scheduledPolls.push({ callback, delay });
      return scheduledPolls.length;
    },
    clearTimeout(handle) {
      clearedPolls.push(handle);
    },
  },
);
assert.equal(pollCount, 1);
assert.equal(scheduledPolls.length, 0);
firstPollResolve();
await firstPoll;
await new Promise((resolve) => setImmediate(resolve));
assert.equal(scheduledPolls.length, 1);
assert.equal(scheduledPolls[0].delay, 2000);
scheduledPolls[0].callback();
assert.equal(pollCount, 2);
assert.equal(scheduledPolls.length, 1);
stopPolling();
secondPollResolve();
await secondPoll;
await new Promise((resolve) => setImmediate(resolve));
assert.equal(scheduledPolls.length, 1);
assert.deepEqual(clearedPolls, []);

const executableApiSource = apiSource
  .replace("import.meta.env.VITE_FINANCE_AGENT_API_BASE", "undefined");
const { outputText: apiOutputText } = ts.transpileModule(executableApiSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});
const apiModuleUrl = `data:text/javascript;base64,${Buffer.from(apiOutputText).toString("base64")}`;
const apiModule = await import(apiModuleUrl);
assert.equal(
  apiModule.taskMonitorRequestErrorMessage(new Error("signal is aborted without reason")),
  "任务监控请求超时，本页会继续自动重试。",
);
assert.equal(
  apiModule.taskMonitorRequestErrorMessage(new Error("网络连接失败")),
  "网络连接失败",
);
const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
let schedulerProgressTimeout = 0;
globalThis.window = {
  setTimeout(_callback, delay) {
    schedulerProgressTimeout = delay;
    return 1;
  },
  clearTimeout() {},
};
globalThis.fetch = async () => {
  throw new Error("signal is aborted without reason");
};
try {
  const unavailableProgress = await apiModule.loadDataSchedulerProgress(2);
  assert.equal(schedulerProgressTimeout, 20000);
  assert.equal(unavailableProgress.status, "unavailable");
  assert.equal(unavailableProgress.message, "任务监控请求超时，本页会继续自动重试。");
} finally {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
}
