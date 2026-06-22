export type TaskMonitorStatus = "running" | "paused" | "waiting" | "completed" | "failed" | "skipped" | "locked" | "unknown";
export type TaskMonitorFilter = "all" | "running" | "paused" | "waiting" | "completed" | "failed";
export type TaskMonitorTone = "green" | "amber" | "blue" | "red";

export type TaskMonitorSummary = {
  totalItems: number;
  completedItems: number;
  runningItems: number;
  failedItems: number;
  retryItems: number;
  remainingItems: number;
};

export type TaskMonitorStage = {
  key: string;
  title: string;
  status: TaskMonitorStatus;
  statusLabel: string;
  tone: TaskMonitorTone;
  totalItems: number;
  completedItems: number;
  failedItems: number;
  progressRatio: number;
  updatedAt: string;
};

export type TaskMonitorEvent = {
  eventType: string;
  jobName: string;
  runId: string;
  stageKey: string;
  symbol: string;
  name: string;
  status: TaskMonitorStatus;
  batchIndex: number;
  batchCount: number;
  itemCount: number;
  retryCount: number;
  retryAfterSeconds: number;
  nextRetryAt: string;
  providerKey: string;
  errorCategory: string;
  errorMessage: string;
  createdAt: string;
};

export type TaskMonitorItem = {
  id: string;
  jobName: string;
  runId: string;
  title: string;
  description: string;
  searchText: string;
  searchKeywords: string[];
  market: string;
  taskType: string;
  priority: number;
  resourcePool: string;
  status: TaskMonitorStatus;
  statusLabel: string;
  tone: TaskMonitorTone;
  intervalSeconds: number;
  startedAt: string;
  updatedAt: string;
  finishedAt: string;
  progressRatio: number;
  batchIndex: number;
  batchCount: number;
  batchSize: number;
  maxWorkers: number;
  throughputPerMinute: number;
  errorMessage: string;
  summary: TaskMonitorSummary;
  stages: TaskMonitorStage[];
  events: TaskMonitorEvent[];
  metrics: Record<string, any>;
  isRealtime: boolean;
};

export type TaskMonitorSourceRateState = {
  sourceKey: string;
  state: string;
  successCount: number;
  failureCount: number;
  timeoutCount: number;
  disconnectCount: number;
  rateLimitedCount: number;
  failureRate: number;
  effectiveMaxConcurrency: number;
  effectiveMinIntervalSeconds: number;
  cooldownRemainingSeconds: number;
  lastErrorMessage: string;
  probeOk: boolean;
  nextRecoverAt: string;
  updatedAt: string;
};

export type TaskMonitorGlobalConcurrency = {
  running: number;
  limit: number;
};

export type TaskMonitorResourcePoolState = {
  name: string;
  running: number;
  queued: number;
  limit: number;
};

export type TaskMonitorModel = {
  connectionStatus: string;
  message: string;
  cacheBackend: string;
  generatedAt: string;
  items: TaskMonitorItem[];
  sourceRateStates: TaskMonitorSourceRateState[];
  globalConcurrency: TaskMonitorGlobalConcurrency;
  resourcePools: Record<string, TaskMonitorResourcePoolState>;
  metrics: {
    runningCount: number;
    pausedCount: number;
    waitingCount: number;
    failedCount: number;
    completedRecentCount: number;
  };
};

export type FallbackWaitingTask = {
  job_name?: string;
  title?: string;
  status?: string;
  interval_seconds?: number;
  [key: string]: unknown;
};

export const taskMonitorFilters: Array<{ id: TaskMonitorFilter; label: string }> = [
  { id: "all", label: "全部" },
  { id: "running", label: "运行中" },
  { id: "paused", label: "已暂停" },
  { id: "waiting", label: "等待中" },
  { id: "completed", label: "已完成" },
  { id: "failed", label: "失败" },
];

const statusMeta: Record<TaskMonitorStatus, { label: string; tone: TaskMonitorTone }> = {
  running: { label: "运行中", tone: "blue" },
  paused: { label: "已暂停", tone: "amber" },
  waiting: { label: "等待中", tone: "amber" },
  completed: { label: "已完成", tone: "green" },
  failed: { label: "失败", tone: "red" },
  skipped: { label: "已跳过", tone: "amber" },
  locked: { label: "排队中", tone: "amber" },
  unknown: { label: "未知", tone: "amber" },
};

const jobDescriptionMap: Record<string, { title: string; description: string; keywords?: string[] }> = {
  "ashare.universe.all": {
    title: "A 股资产池刷新",
    description: "获取全市场股票、指数/行业/概念成员和热度种子，写入资产与 Universe 关系。",
    keywords: ["assets", "asset_universes", "asset_universe_members", "market_calendars", "calendar"],
  },
  "ashare.bars.1d.bootstrap": {
    title: "A 股 10 年历史日 K 初始化",
    description: "手动执行的一次性初始化任务，补齐最近 10 年 A 股前复权日 K；不会自动进入周期调度。",
    keywords: ["market_bars", "bootstrap", "历史日K", "K线"],
  },
  "ashare.bars.1d": {
    title: "A 股日 K 线补采",
    description: "按股票批次增量同步前复权日线行情，用于技术指标和推荐计算。",
    keywords: ["market_bars", "日K", "K线"],
  },
  "ashare.bars.1d.midday_partial": {
    title: "A 股午盘临时日 K 同步",
    description: "交易日午盘后同步当天临时日 K，写入 partial 状态；用于午盘后的数据校验和观察。",
    keywords: ["market_bars", "midday_partial", "午盘", "日K", "K线"],
  },
  "ashare.bars.1d.close_final": {
    title: "A 股收盘最终日 K 同步",
    description: "交易日收盘后同步当天最终日 K，写入 available 状态；用于当日指标、风险和推荐计算。",
    keywords: ["market_bars", "close_final", "收盘", "日K", "K线"],
  },
  "ashare.bars.1d.revision": {
    title: "A 股凌晨日 K 修正",
    description: "凌晨低频修正近 7 日 K 线、复权变化和漏采记录，优先补齐失败或过期标的。",
    keywords: ["market_bars", "revision", "复权", "补漏", "日K", "K线"],
  },
  "ashare.realtime_quotes": {
    title: "A 股实时行情快照",
    description: "刷新最新价格、涨跌幅、成交额和交易状态，供观察池与风险判断使用。",
  },
  "ashare.fundamentals": {
    title: "A 股基本面与估值",
    description: "同步财务指标、业绩报告和估值数据，补齐 fundamental / valuation 因子。",
    keywords: ["fundamental_snapshots", "factor_frames", "基本面", "估值"],
  },
  "ashare.capital_flow": {
    title: "A 股资金流同步",
    description: "同步个股资金流向和排名数据，补齐 capital_flow 因子。",
    keywords: ["capital_flow_snapshots", "factor_frames", "资金流"],
  },
  "ashare.events": {
    title: "A 股新闻公告同步",
    description: "同步新闻、公告和事件线索，后续用于事件清洗、衰减和 Agent 新闻检索。",
    keywords: ["event_records", "evidence", "news", "notice", "新闻", "公告"],
  },
  "ashare.news_articles": {
    title: "A 股新闻正文补抓",
    description: "低并发补抓已入库新闻的原文正文，回填事件和证据 payload，不阻塞新闻列表同步。",
  },
  "ashare.risk_sentiment": {
    title: "A 股风险与情绪",
    description: "同步停复牌、热榜、涨停池、龙虎榜、大宗交易和两融等风险情绪数据。",
    keywords: ["risk_findings", "event_records", "风险", "情绪"],
  },
  "fund.universe.all": {
    title: "基金资产池刷新",
    description: "统一刷新 ETF、LOF 和开放式基金列表，维护基金 Universe 与资产主表。",
  },
  "fund.etf.bars.1d.bootstrap": {
    title: "ETF 10 年日 K 初始化",
    description: "手动执行 ETF 历史日 K 建库任务，补齐长周期趋势分析所需行情。",
    keywords: ["market_bars", "fund", "ETF", "历史日K", "K线"],
  },
  "fund.lof.bars.1d.bootstrap": {
    title: "LOF 10 年日 K 初始化",
    description: "手动执行 LOF 历史日 K 建库任务，补齐场内基金长期走势数据。",
    keywords: ["market_bars", "fund", "LOF", "历史日K", "K线"],
  },
  "fund.bars.1d.close_final": {
    title: "基金收盘最终日 K",
    description: "交易日收盘后刷新 ETF/LOF 最近窗口内的最终日 K，供基金因子与观察使用。",
    keywords: ["market_bars", "fund", "close_final", "基金日K", "K线"],
  },
  "fund.open.nav.bootstrap": {
    title: "开放式基金 10 年净值初始化",
    description: "手动执行开放式基金净值建库任务，形成长期净值曲线。",
  },
  "fund.open.nav.daily": {
    title: "开放式基金每日净值",
    description: "夜间刷新开放式基金最新净值，并在次日凌晨补一次修正。",
  },
  "crypto_future.universe.binance": {
    title: "Binance 合约资产池",
    description: "同步 USDT 合约交易对清单，维护数字货币合约 Universe。",
  },
  "crypto_future.bars.1h": {
    title: "Binance 合约 1 小时 K 线",
    description: "按交易对批次同步合约小时线行情，供趋势和波动因子使用。",
    keywords: ["market_bars", "crypto_future", "1h", "小时K", "K线"],
  },
  "crypto_future.derivatives": {
    title: "Binance 合约衍生品快照",
    description: "同步资金费率、持仓量和多空比等衍生品指标。",
  },
  "crypto_spot.universe.binance": {
    title: "Binance 现货资产池",
    description: "同步 USDT 现货交易对清单，维护数字货币现货 Universe。",
  },
  "crypto_spot.bars.1h": {
    title: "Binance 现货 1 小时 K 线",
    description: "按交易对批次同步现货小时线行情，供数字货币指标计算使用。",
    keywords: ["market_bars", "crypto_spot", "1h", "小时K", "K线"],
  },
  "quality.ashare": {
    title: "A 股数据质量检查",
    description: "检查 A 股资产、行情、基本面、资金流和事件数据覆盖率，识别缺口与过期数据。",
    keywords: ["data_quality", "质量检查", "coverage"],
  },
  "quality.crypto_future": {
    title: "合约数据质量检查",
    description: "检查 Binance 合约资产、小时线和衍生品指标覆盖率，识别缺口与异常数据。",
  },
  "quality.crypto_spot": {
    title: "现货数据质量检查",
    description: "检查 Binance 现货资产和小时线行情覆盖率，识别缺口与异常数据。",
  },
  "analytics.recommendations.ashare.all_a": {
    title: "A 股全市场推荐计算",
    description: "基于最新行情、因子、风险和数据质量生成 A 股候选池评分与推荐结果。",
    keywords: [
      "indicator_frames",
      "factor_frames",
      "screening_results",
      "asset_scores",
      "signal_snapshots",
      "asset_recommendations",
    ],
  },
  "analytics.recommendations.crypto_future.binance": {
    title: "Binance 合约推荐计算",
    description: "基于合约行情、衍生品指标和风险信号生成数字货币合约候选推荐。",
  },
  "analytics.recommendations.crypto_spot.binance": {
    title: "Binance 现货推荐计算",
    description: "基于现货行情、趋势因子和风险信号生成数字货币现货候选推荐。",
  },
  "analytics.technical_screening.ashare.main_board": {
    title: "A 股主板技术初筛",
    description: "基于最新日 K 计算技术指标并写入 indicator_frames，为主板股票生成技术初筛结果。",
    keywords: ["indicator_frames", "screening_results", "technical_screening", "技术指标", "初筛"],
  },
  "analytics.technical_screening.fund.exchange_traded": {
    title: "场内基金技术初筛",
    description: "基于 ETF/LOF 日 K 计算技术指标并生成基金技术初筛结果。",
    keywords: ["indicator_frames", "screening_results", "fund", "技术指标", "初筛"],
  },
  "analytics.universe.merge.ashare.recommendation": {
    title: "A 股推荐候选池合并",
    description: "合并技术初筛、研究池和回避池规则，生成推荐流水线使用的 A 股候选 Universe。",
    keywords: ["screening_results", "asset_scores", "recommendation", "candidate_universe"],
  },
  "analytics.universe.rebuild_avoid_pool.ashare": {
    title: "A 股回避池重建",
    description: "根据风险发现、质押、解禁和质量问题重建回避池，供推荐计算剔除高风险标的。",
    keywords: ["risk_findings", "avoid_pool", "回避池"],
  },
  "analytics.triggers.evaluate.daily": {
    title: "每日触发事件评估",
    description: "读取推荐、风险、观察池和数据质量变化，生成需要 Agent 消费的触发事件。",
    keywords: ["trigger_events", "asset_recommendations", "risk_findings", "data_quality"],
  },
  "analytics.triggers.evaluate.intraday": {
    title: "盘中触发事件评估",
    description: "读取盘中急跌、放量和持仓变化，生成盘中 Agent 触发事件。",
    keywords: ["trigger_events", "realtime_quote_snapshots", "intraday"],
  },
  "agent.loop.consume.after_trigger": {
    title: "触发后 Agent 消费",
    description: "消费触发事件并调用 Agent 工具链生成分析结果。",
    keywords: ["agent_analysis_runs", "decision_logs", "trigger_events"],
  },
  "agent.loop.consume.sweep": {
    title: "Agent 事件巡检消费",
    description: "定时扫描未消费触发事件，补偿执行 Agent 分析。",
    keywords: ["agent_analysis_runs", "trigger_events"],
  },
  "analytics.high_risk_reviews.after_agent": {
    title: "Agent 后高风险复核",
    description: "对 Agent 生成的高风险决策进行模型复核和审计回写。",
    keywords: ["model_reviews", "decision_logs", "high_risk_reviews"],
  },
  "analytics.high_risk_reviews.sweep": {
    title: "高风险复核巡检",
    description: "定时扫描待复核决策并执行高风险模型复核。",
    keywords: ["model_reviews", "decision_logs", "high_risk_reviews"],
  },
  "analytics.reviews.due": {
    title: "到期复盘任务",
    description: "扫描到期的人工执行复盘任务并沉淀复盘结果。",
    keywords: ["review_tasks", "execution_reviews", "finance_memory"],
  },
};

export function buildTaskMonitorModel(
  response: Record<string, any> | null | undefined,
  options: { fallbackWaiting?: FallbackWaitingTask[] } = {},
): TaskMonitorModel {
  const data = response?.data ?? {};
  const tasks = asArray(data.tasks).map((task) => normalizeTaskItem(task, true));
  const waiting = asArray(data.waiting).map((task) =>
    normalizeTaskItem({ ...task, status: task?.status ?? "waiting" }, false),
  );
  const fallbackWaiting = asArray(options.fallbackWaiting).map((task) =>
    normalizeTaskItem({ ...task, status: task?.status ?? "waiting" }, false),
  );
  const items = dedupeTaskItems([...tasks, ...waiting, ...fallbackWaiting]);
  const rawMetrics = data.metrics ?? {};
  const rawGlobalConcurrency = data.global_concurrency ?? {};
  return {
    connectionStatus: String(response?.status ?? "unavailable"),
    message: String(response?.message ?? ""),
    cacheBackend: String(data.cache_backend ?? ""),
    generatedAt: String(data.generated_at ?? ""),
    items,
    sourceRateStates: asArray(data.source_rate_states).map(normalizeSourceRateState),
    globalConcurrency: {
      running: firstNumber(rawGlobalConcurrency.running, countByStatus(items, "running")),
      limit: firstNumber(rawGlobalConcurrency.limit, 0),
    },
    resourcePools: normalizeResourcePools(data.resource_pools),
    metrics: {
      runningCount: firstNumber(rawMetrics.running_count, countByStatus(items, "running")),
      pausedCount: firstNumber(rawMetrics.paused_count, countByStatus(items, "paused")),
      waitingCount: firstNumber(rawMetrics.waiting_count, countByStatus(items, "waiting")),
      failedCount: firstNumber(rawMetrics.failed_count, countByStatus(items, "failed")),
      completedRecentCount: firstNumber(
        rawMetrics.completed_recent_count,
        countByStatus(items, "completed"),
      ),
    },
  };
}

export function filterTaskMonitorItems(
  items: TaskMonitorItem[],
  statusFilter: TaskMonitorFilter,
  query: string,
): TaskMonitorItem[] {
  const normalizedQuery = query.trim().toLowerCase();
  return items.filter((item) => {
    const matchesStatus = statusFilter === "all" || item.status === statusFilter;
    const matchesQuery =
      !normalizedQuery ||
      item.jobName.toLowerCase().includes(normalizedQuery) ||
      item.title.toLowerCase().includes(normalizedQuery) ||
      item.description.toLowerCase().includes(normalizedQuery) ||
      item.taskType.toLowerCase().includes(normalizedQuery) ||
      item.searchText.toLowerCase().includes(normalizedQuery) ||
      item.searchKeywords.some((keyword) => keyword.toLowerCase() === normalizedQuery);
    return matchesStatus && matchesQuery;
  });
}

export function extractFallbackWaitingTasks(
  config: Record<string, any> | null | undefined,
  status: Record<string, any> | null | undefined,
): FallbackWaitingTask[] {
  const queuedJobs = normalizeJobNames(status?.health?.payload?.queued_jobs);
  const runningJobs = normalizeJobNames(status?.health?.payload?.running_jobs);
  const queueTasks = [...runningJobs, ...queuedJobs].map((jobName) => ({
    job_name: jobName,
    title: jobName,
    status: runningJobs.includes(jobName) ? "running" : "waiting",
  }));
  const previewTasks = asArray(config?.data?.preview?.tasks).map((task) => ({
    job_name: String(task.job_name ?? task.task_key ?? task.name ?? task.title ?? ""),
    title: String(task.title ?? task.job_name ?? task.task_key ?? task.name ?? ""),
    interval_seconds: firstNumber(task.interval_seconds, 0),
    status: "waiting",
  }));
  const seen = new Set<string>();
  return [...queueTasks, ...previewTasks].filter((task) => {
    const key = String(task.job_name ?? task.title ?? "").trim();
    if (!key || seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

export function extractSchedulerJobTasks(
  jobsResponse: Record<string, any> | null | undefined,
): FallbackWaitingTask[] {
  return asArray(jobsResponse?.data?.jobs)
    .filter((job) => {
      const jobName = String(job?.name ?? job?.job_name ?? "").trim();
      const scheduleType = String(job?.schedule_type ?? "").trim();
      return Boolean(jobName) && (Boolean(job?.enabled) || scheduleType === "manual");
    })
    .map((job) => ({
      job_name: String(job?.name ?? job?.job_name ?? ""),
      title: String(job?.title ?? job?.name ?? job?.job_name ?? ""),
      interval_seconds: firstNumber(job?.interval_seconds, 0),
      priority: firstNumber(job?.priority, 100),
      resource_pool: String(job?.resource_pool ?? "default"),
      task_type: String(job?.params?.sync_task_type ?? ""),
      market: String(job?.market ?? ""),
      params: job?.params,
      status: "waiting",
    }));
}

export function statusLabel(status: string | undefined): string {
  return statusMeta[normalizeStatus(status)].label;
}

export function statusTone(status: string | undefined): TaskMonitorTone {
  return statusMeta[normalizeStatus(status)].tone;
}

export function formatTaskLogLine(event: TaskMonitorEvent): string {
  const time = formatTime(event.createdAt);
  const label = eventLabel(event.eventType, event.status);
  const target = [event.symbol, event.name].filter(Boolean).join(" ");
  const batch =
    event.batchIndex > 0 && event.batchCount > 0
      ? ` · 批次 ${event.batchIndex}/${event.batchCount}`
      : "";
  const retry =
    event.nextRetryAt || event.retryAfterSeconds > 0
      ? ` · 下次重试 ${event.nextRetryAt ? formatDateTime(event.nextRetryAt) : formatDuration(event.retryAfterSeconds)}`
      : "";
  const provider = event.providerKey ? ` · ${event.providerKey}` : "";
  const error = event.errorMessage ? ` · ${event.errorMessage}` : "";
  return `[${time}] ${[label, target].filter(Boolean).join(" ")}${batch}${retry}${provider}${error}`;
}

export function formatDuration(seconds: unknown): string {
  const value = Math.max(0, Math.round(firstNumber(seconds, 0)));
  if (value < 60) {
    return `${value}s`;
  }
  if (value < 3600) {
    return `${Math.floor(value / 60)}m ${value % 60}s`;
  }
  return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
}

export function estimateRemainingSeconds(
  explicitSeconds: unknown,
  completedItems: unknown,
  remainingItems: unknown,
  durationSeconds: unknown,
): number | null {
  const explicitValue = firstNumber(explicitSeconds, -1);
  if (explicitValue > 0) {
    return Math.round(explicitValue);
  }
  const completed = firstNumber(completedItems, 0);
  const remaining = firstNumber(remainingItems, 0);
  const duration = firstNumber(durationSeconds, 0);
  if (completed <= 0 || remaining <= 0 || duration <= 0) {
    return null;
  }
  return Math.round((duration / completed) * remaining);
}

export function formatPercent(ratio: unknown): string {
  return `${Math.round(clampRatio(firstNumber(ratio, 0)) * 100)}%`;
}

export function formatCompactNumber(value: unknown): string {
  const numeric = firstNumber(value, 0);
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(numeric);
}

export function formatDateTime(value: string | undefined): string {
  if (!value) {
    return "-";
  }
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})(?::\d{2})?/);
  if (match) {
    return `${match[1]}-${match[2]}-${match[3]} ${match[4]}`;
  }
  return value;
}

function normalizeTaskItem(raw: Record<string, any>, isRealtime: boolean): TaskMonitorItem {
  const rawSummary = raw.summary ?? raw;
  const jobName = String(raw.job_name ?? raw.jobName ?? raw.name ?? raw.task_key ?? "").trim();
  const jobDescription = lookupSchedulerJobMetadata(jobName);
  const rawTitle = String(raw.title ?? "").trim();
  const title =
    !rawTitle || rawTitle === jobName
      ? String(jobDescription.title ?? (jobName || "未命名任务"))
      : rawTitle;
  const description = String(raw.description ?? raw.detail ?? jobDescription.description ?? "");
  const searchText = [
    jobName,
    title,
    description,
    raw.task_type,
    raw.taskType,
    raw.market,
  ]
    .filter(Boolean)
    .join(" ");
  const searchKeywords = (jobDescription.keywords ?? []).map((keyword) => String(keyword));
  const summary: TaskMonitorSummary = {
    totalItems: firstNumber(rawSummary.total_items, raw.total_items, 0),
    completedItems: firstNumber(rawSummary.completed_items, raw.completed_items, 0),
    runningItems: firstNumber(rawSummary.running_items, raw.running_items, 0),
    failedItems: firstNumber(rawSummary.failed_items, raw.failed_items, 0),
    retryItems: firstNumber(rawSummary.retry_items, raw.retry_items, 0),
    remainingItems: firstNumber(rawSummary.remaining_items, raw.remaining_items, 0),
  };
  const countsCompleted =
    summary.totalItems > 0 &&
    summary.completedItems >= summary.totalItems &&
    summary.runningItems <= 0 &&
    summary.remainingItems <= 0 &&
    summary.failedItems <= 0;
  const explicitProgressSource = rawSummary.progress_ratio ?? raw.progress_ratio;
  const derivedProgress = summary.totalItems > 0 ? summary.completedItems / summary.totalItems : 0;
  const progressRatio = countsCompleted
    ? 1
    : clampRatio(
        explicitProgressSource === undefined || explicitProgressSource === null || explicitProgressSource === ""
          ? derivedProgress
          : firstNumber(explicitProgressSource, derivedProgress),
      );
  const status = normalizeTaskStatus(raw.status, summary, progressRatio);
  return {
    id: String(raw.run_id ?? raw.runId ?? jobName ?? title),
    jobName,
    runId: String(raw.run_id ?? raw.runId ?? ""),
    title,
    description,
    searchText,
    searchKeywords,
    market: String(raw.market ?? ""),
    taskType: String(raw.task_type ?? raw.taskType ?? ""),
    priority: firstNumber(raw.priority, raw.metrics?.priority, 100),
    resourcePool: String(raw.resource_pool ?? raw.resourcePool ?? raw.metrics?.resource_pool ?? "default"),
    status,
    statusLabel: statusMeta[status].label,
    tone: statusMeta[status].tone,
    intervalSeconds: firstNumber(raw.interval_seconds, raw.intervalSeconds, 0),
    startedAt: String(raw.started_at ?? raw.startedAt ?? ""),
    updatedAt: String(raw.updated_at ?? raw.updatedAt ?? ""),
    finishedAt: String(raw.finished_at ?? raw.finishedAt ?? ""),
    progressRatio,
    batchIndex: firstNumber(raw.batch_index, raw.batchIndex, 0),
    batchCount: firstNumber(raw.batch_count, raw.batchCount, 0),
    batchSize: firstNumber(raw.batch_size, raw.batchSize, 0),
    maxWorkers: firstNumber(raw.max_workers, raw.maxWorkers, raw.metrics?.max_workers, 0),
    throughputPerMinute: firstNumber(
      raw.throughput_per_minute,
      raw.throughputPerMinute,
      raw.metrics?.throughput_per_minute,
      0,
    ),
    errorMessage: String(raw.error_message ?? raw.errorMessage ?? ""),
    summary,
    stages: asArray(raw.stages).map(normalizeStage),
    events: asArray(raw.recent_events ?? raw.events).map(normalizeEvent),
    metrics: raw.metrics ?? {},
    isRealtime,
  };
}

export function describeSchedulerJob(
  jobName: string | undefined,
): { title: string; description: string } {
  const metadata = lookupSchedulerJobMetadata(jobName);
  return {
    title: metadata.title,
    description: metadata.description,
  };
}

function lookupSchedulerJobMetadata(
  jobName: string | undefined,
): { title: string; description: string; keywords?: string[] } {
  const normalizedJobName = String(jobName ?? "").trim();
  if (!normalizedJobName) {
    return { title: "未命名任务", description: "" };
  }
  const mapped = jobDescriptionMap[normalizedJobName];
  if (mapped) {
    return mapped;
  }
  if (normalizedJobName.includes(".bars.")) {
    return {
      title: `${normalizedJobName} 行情 K 线同步`,
      description: "增量同步行情 K 线数据，用于技术指标、风险判断和推荐排序。",
    };
  }
  if (normalizedJobName.includes(".universe.")) {
    return {
      title: `${normalizedJobName} 资产池刷新`,
      description: "刷新市场资产池和成员关系，为后续行情、因子和推荐任务提供标的范围。",
    };
  }
  if (normalizedJobName.startsWith("quality.")) {
    return {
      title: `${normalizedJobName} 数据质量检查`,
      description: "检查对应市场的数据覆盖率、缺口、过期和异常情况，为后续因子与推荐计算提供质量依据。",
    };
  }
  if (normalizedJobName.startsWith("analytics.recommendations.")) {
    return {
      title: `${normalizedJobName} 推荐计算`,
      description: "读取最新基础数据、因子、风险和质量结果，生成候选池评分、信号和推荐排序。",
    };
  }
  return {
    title: normalizedJobName,
    description: "调度器任务，正在等待执行或写入实时进度。",
  };
}

function normalizeResourcePools(raw: unknown): Record<string, TaskMonitorResourcePoolState> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(raw).map(([name, value]) => {
      const payload = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
      return [
        name,
        {
          name,
          running: firstNumber(payload.running, 0),
          queued: firstNumber(payload.queued, 0),
          limit: firstNumber(payload.limit, payload.max_concurrent_jobs, 0),
        },
      ];
    }),
  );
}

function normalizeStage(raw: Record<string, any>): TaskMonitorStage {
  const status = normalizeStatus(raw.status);
  return {
    key: String(raw.stage_key ?? raw.key ?? raw.title ?? ""),
    title: String(raw.title ?? raw.stage_key ?? "未命名阶段"),
    status,
    statusLabel: statusMeta[status].label,
    tone: statusMeta[status].tone,
    totalItems: firstNumber(raw.total_items, 0),
    completedItems: firstNumber(raw.completed_items, 0),
    failedItems: firstNumber(raw.failed_items, 0),
    progressRatio: clampRatio(firstNumber(raw.progress_ratio, 0)),
    updatedAt: String(raw.updated_at ?? ""),
  };
}

function normalizeEvent(raw: Record<string, any>): TaskMonitorEvent {
  return {
    eventType: String(raw.event_type ?? raw.type ?? ""),
    jobName: String(raw.job_name ?? ""),
    runId: String(raw.run_id ?? ""),
    stageKey: String(raw.stage_key ?? ""),
    symbol: String(raw.symbol ?? ""),
    name: String(raw.name ?? ""),
    status: normalizeStatus(raw.status),
    batchIndex: firstNumber(raw.batch_index, 0),
    batchCount: firstNumber(raw.batch_count, 0),
    itemCount: firstNumber(raw.item_count, 0),
    retryCount: firstNumber(raw.retry_count, 0),
    retryAfterSeconds: firstNumber(raw.retry_after_seconds, raw.retryAfterSeconds, 0),
    nextRetryAt: String(raw.next_retry_at ?? raw.nextRetryAt ?? ""),
    providerKey: String(raw.provider_key ?? raw.providerKey ?? ""),
    errorCategory: String(raw.error_category ?? raw.errorCategory ?? ""),
    errorMessage: String(raw.error_message ?? ""),
    createdAt: String(raw.created_at ?? ""),
  };
}

function normalizeSourceRateState(raw: Record<string, any>): TaskMonitorSourceRateState {
  return {
    sourceKey: String(raw.source_key ?? raw.sourceKey ?? ""),
    state: String(raw.state ?? ""),
    successCount: firstNumber(raw.success_count, raw.successCount, 0),
    failureCount: firstNumber(raw.failure_count, raw.failureCount, 0),
    timeoutCount: firstNumber(raw.timeout_count, raw.timeoutCount, 0),
    disconnectCount: firstNumber(raw.disconnect_count, raw.disconnectCount, 0),
    rateLimitedCount: firstNumber(raw.rate_limited_count, raw.rateLimitedCount, 0),
    failureRate: clampRatio(firstNumber(raw.failure_rate, raw.failureRate, 0)),
    effectiveMaxConcurrency: firstNumber(
      raw.effective_max_concurrency,
      raw.effectiveMaxConcurrency,
      0,
    ),
    effectiveMinIntervalSeconds: firstNumber(
      raw.effective_min_interval_seconds,
      raw.effectiveMinIntervalSeconds,
      0,
    ),
    cooldownRemainingSeconds: firstNumber(
      raw.cooldown_remaining_seconds,
      raw.cooldownRemainingSeconds,
      0,
    ),
    lastErrorMessage: String(raw.last_error_message ?? raw.lastErrorMessage ?? ""),
    probeOk: Boolean(raw.probe_ok ?? raw.probeOk ?? false),
    nextRecoverAt: String(raw.next_recover_at ?? raw.nextRecoverAt ?? ""),
    updatedAt: String(raw.updated_at ?? raw.updatedAt ?? ""),
  };
}

function normalizeStatus(status: string | undefined): TaskMonitorStatus {
  const value = String(status ?? "").toLowerCase();
  if (["running", "started", "processing", "active"].includes(value)) {
    return "running";
  }
  if (["paused", "pause", "suspended"].includes(value)) {
    return "paused";
  }
  if (["waiting", "queued", "pending", "idle", "scheduled"].includes(value)) {
    return "waiting";
  }
  if (["completed", "success", "succeeded", "done", "finished"].includes(value)) {
    return "completed";
  }
  if (["failed", "error", "errored", "failure"].includes(value)) {
    return "failed";
  }
  if (["skipped", "skip"].includes(value)) {
    return "skipped";
  }
  if (["locked", "lock"].includes(value)) {
    return "locked";
  }
  return "unknown";
}

function normalizeTaskStatus(
  status: string | undefined,
  summary: TaskMonitorSummary,
  progressRatio: number,
): TaskMonitorStatus {
  const normalized = normalizeStatus(status);
  if (
    ["running", "paused", "waiting", "completed", "failed", "skipped", "locked"].includes(
      normalized,
    )
  ) {
    return normalized;
  }
  if (
    summary.totalItems > 0 &&
    summary.completedItems >= summary.totalItems &&
    summary.runningItems <= 0 &&
    summary.remainingItems <= 0 &&
    summary.failedItems <= 0 &&
    progressRatio >= 1
  ) {
    return "completed";
  }
  return normalized;
}

function normalizeJobNames(value: unknown): string[] {
  return (Array.isArray(value) ? value : [])
    .map((item) => {
      if (typeof item === "string") {
        return item;
      }
      return String(item?.name ?? item?.job_name ?? item ?? "");
    })
    .map((item) => item.trim())
    .filter(Boolean);
}

function eventLabel(eventType: string, status: TaskMonitorStatus): string {
  if (eventType === "symbol_completed" && status === "skipped") {
    return "跳过";
  }
  if (eventType === "symbol_completed" && status === "locked") {
    return "排队";
  }
  const map: Record<string, string> = {
    job_started: "任务开始",
    stage_started: "阶段开始",
    batch_started: "批次开始",
    symbol_started: "开始",
    symbol_completed: "完成",
    symbol_failed: "失败",
    symbol_retry: "重试",
    batch_completed: "批次完成",
    job_completed: "任务完成",
    job_failed: "任务失败",
    job_paused: "任务暂停",
    job_resumed: "任务继续",
  };
  return map[eventType] ?? statusMeta[status].label;
}

function formatTime(value: string): string {
  const match = value.match(/T(\d{2}:\d{2}:\d{2})/);
  if (match) {
    return match[1];
  }
  return "--:--:--";
}

function dedupeTaskItems(items: TaskMonitorItem[]): TaskMonitorItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = item.jobName || item.id;
    if (!key || seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function countByStatus(items: TaskMonitorItem[], status: TaskMonitorStatus): number {
  return items.filter((item) => item.status === status).length;
}

function clampRatio(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(1, Math.max(0, value));
}

function firstNumber(...values: unknown[]): number {
  for (const value of values) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return numeric;
    }
  }
  return 0;
}

function asArray(value: unknown): Record<string, any>[] {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === "object") : [];
}
