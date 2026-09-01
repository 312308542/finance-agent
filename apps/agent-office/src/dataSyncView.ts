export type SchedulerSummary = {
  tone: "green" | "amber" | "blue" | "red";
  label: string;
  detail: string;
};

export type SchedulerStartFeedback = {
  statusText: string;
  modeLabel: string;
  writePolicy: string;
};

export type SchedulerQueueSummary = {
  maxConcurrentJobs: number;
  runningJobs: string[];
  queuedJobs: string[];
};

export const schedulerRuntimeStatuses = [
  "scheduled",
  "blocked",
  "pending",
  "running",
  "completed",
  "failed",
  "cancelled",
] as const;

export type SchedulerRuntimeStatus = (typeof schedulerRuntimeStatuses)[number];

export type SchedulerRuntimeTask = Record<string, any> & {
  task_id: string;
  job_name: string;
  status: SchedulerRuntimeStatus;
};

export type SchedulerRuntimeSummary = {
  source: string;
  statusCounts: Record<SchedulerRuntimeStatus, number>;
  listedStatusCounts: Record<SchedulerRuntimeStatus, number>;
  taskList: Record<string, any>;
  tasks: SchedulerRuntimeTask[];
  configDrift: boolean;
  configDriftStatus: string;
  schedulerConfigDigest: string | null;
  apiConfigDigest: string | null;
  resourcePools: Record<string, Record<string, any>>;
  metrics: Record<string, any>;
};

const marketOrder = ["ashare", "fund", "crypto_spot", "crypto_future"];
const presetMarketDefaults: Record<string, string[]> = {
  "personal-ashare": ["ashare", "fund"],
  "personal-comprehensive": ["ashare", "fund", "crypto_spot", "crypto_future"],
  "ashare-comprehensive": ["ashare"],
  "crypto-comprehensive": ["crypto_spot", "crypto_future"],
  lightweight: ["ashare", "fund", "crypto_spot"],
};
const processingStatusText: Record<string, string> = {
  active_with_collection: "随采集执行",
  implemented_not_scheduled: "已实现未调度",
  requires_universe_selection: "需选择候选池",
  covered_by_collection_jobs: "采集任务覆盖",
  disabled: "未启用",
};

function normalizeJobNames(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => {
      if (typeof item === "string") {
        return item;
      }
      if (item && typeof item === "object" && "name" in item) {
        return String((item as { name?: unknown }).name ?? "");
      }
      return String(item ?? "");
    })
    .map((item) => item.trim())
    .filter(Boolean);
}

export function pickEnabledMarkets(config: Record<string, any> | undefined): string[] {
  const markets = config?.markets ?? {};
  return marketOrder.filter((market) => markets[market]?.enabled);
}

export function marketsForPreset(preset: string | undefined): string[] {
  return [
    ...(presetMarketDefaults[preset ?? ""] ?? presetMarketDefaults["personal-ashare"]),
  ];
}

export function filterPreviewTasksByMarkets<T extends Record<string, any>>(
  tasks: T[],
  markets: string[],
): T[] {
  const enabledMarkets = new Set(markets);
  return tasks.filter((task) => {
    const market = String(task.market ?? "").trim();
    return !market || enabledMarkets.has(market);
  });
}

export function summarizeSchedulerStatus(status: Record<string, any> | null | undefined): SchedulerSummary {
  const health = status?.health ?? {};
  const process = status?.process ?? {};
  const running = Boolean(process.running);
  const lastJob = health.last_job ?? "暂无任务";
  const lastJobStatus = health.last_job_status ?? health.status ?? "unknown";
  const managedBy = status?.managed_by ?? process.managed_by;
  const service = status?.service ?? process.service;
  const processLabel =
    managedBy === "docker-compose"
      ? `Docker ${service ?? "finance-agent-scheduler"}`
      : process.pid
        ? `PID ${process.pid}`
        : "未启动进程";

  if (status?.status === "degraded" && health.database_status === "error") {
    return {
      tone: "red",
      label: "状态降级",
      detail: `${processLabel} · PostgreSQL 状态不可用，调度器存活未知`,
    };
  }
  if (running) {
    return {
      tone: "green",
      label: "运行中",
      detail: `${processLabel} · ${lastJob} / ${lastJobStatus}`,
    };
  }
  if (health.status === "missing") {
    return {
      tone: "amber",
      label: "存活未知",
      detail: `${processLabel} · 未收到调度器心跳`,
    };
  }
  if (health.healthy) {
    return {
      tone: "blue",
      label: "最近健康",
      detail: `${processLabel} · ${lastJob} / ${lastJobStatus}`,
    };
  }
  return {
    tone: "red",
    label: "需要处理",
    detail: `${processLabel} · ${lastJob} / ${lastJobStatus}`,
  };
}

export function processingStatusLabel(status: string | undefined): string {
  if (!status) {
    return "未知";
  }
  return processingStatusText[status] ?? status;
}

export function schedulerStartFeedback(
  dryRun: boolean,
  result: Record<string, any>,
): SchedulerStartFeedback {
  const message = typeof result.message === "string" ? result.message : "";
  const writesEnabled = Boolean(result.data?.writes_enabled);
  if (result.status !== "ok") {
    return {
      statusText: message || "启动失败",
      modeLabel: "启动失败",
      writePolicy: "未启动",
    };
  }
  if (result.data?.managed_by === "docker-compose") {
    return {
      statusText: message || "Docker 调度器已由容器托管",
      modeLabel: "Docker 托管",
      writePolicy: "容器执行",
    };
  }
  if (dryRun || !writesEnabled) {
    return {
      statusText: message || "预演完成：只生成计划，不写入数据库",
      modeLabel: "预演模式",
      writePolicy: "不入库",
    };
  }
  return {
    statusText: message || "真实同步已启动：采集结果会写入数据库",
    modeLabel: "真实同步",
    writePolicy: "会入库",
  };
}

export function summarizeSchedulerWritePolicy(
  status: Record<string, any> | null | undefined,
): SchedulerStartFeedback {
  const health = status?.health ?? {};
  const process = status?.process ?? {};
  const payload = health.payload ?? {};
  const processState = process.state ?? health.state ?? payload.state;
  const running = Boolean(process.running) || processState === "running";
  const dryRun =
    typeof process.dry_run === "boolean"
      ? process.dry_run
      : typeof payload.dry_run === "boolean"
        ? payload.dry_run
        : undefined;

  if (dryRun === true) {
    return {
      statusText: running
        ? "预演调度运行中：只生成计划，不会写入数据库"
        : "最近一次为预演调度：只生成计划，未写入数据库",
      modeLabel: "预演模式",
      writePolicy: "不入库",
    };
  }
  if (dryRun === false) {
    return {
      statusText: running
        ? "真实数据同步运行中：采集结果会写入数据库"
        : "最近一次为真实同步：任务执行过会写入数据库",
      modeLabel: "真实同步",
      writePolicy: "会入库",
    };
  }
  return {
    statusText: "等待启动调度器",
    modeLabel: "未启动",
    writePolicy: "等待操作",
  };
}

export function summarizeSchedulerQueue(
  status: Record<string, any> | null | undefined,
  fallbackMaxConcurrentJobs = 4,
): SchedulerQueueSummary {
  const payload = status?.health?.payload ?? {};
  return {
    maxConcurrentJobs: Number(payload.max_concurrent_jobs ?? fallbackMaxConcurrentJobs) || fallbackMaxConcurrentJobs,
    runningJobs: normalizeJobNames(payload.running_jobs),
    queuedJobs: normalizeJobNames(payload.queued_jobs),
  };
}

export function summarizeSchedulerRuntime(
  payload: Record<string, any> | null | undefined,
): SchedulerRuntimeSummary {
  const runtime = payload?.runtime ?? payload?.data ?? {};
  const rawCounts = runtime.status_counts ?? {};
  const statusCounts = Object.fromEntries(
    schedulerRuntimeStatuses.map((status) => [status, Math.max(0, Number(rawCounts[status] ?? 0) || 0)]),
  ) as Record<SchedulerRuntimeStatus, number>;
  const tasks = Array.isArray(runtime.tasks)
    ? runtime.tasks.filter(
        (task: any): task is SchedulerRuntimeTask =>
          task &&
          typeof task === "object" &&
          schedulerRuntimeStatuses.includes(task.status as SchedulerRuntimeStatus),
      )
    : [];
  const rawListedCounts = runtime.listed_status_counts ?? {};
  const listedStatusCounts = Object.fromEntries(
    schedulerRuntimeStatuses.map((status) => [
      status,
      Math.max(
        0,
        Number(
          rawListedCounts[status] ??
            tasks.filter((task: SchedulerRuntimeTask) => task.status === status).length,
        ) || 0,
      ),
    ]),
  ) as Record<SchedulerRuntimeStatus, number>;
  return {
    source: String(runtime.source ?? "unavailable"),
    statusCounts,
    listedStatusCounts,
    taskList: runtime.task_list && typeof runtime.task_list === "object" ? runtime.task_list : {},
    tasks,
    configDrift: runtime.config_drift === true,
    configDriftStatus: String(runtime.config_drift_status ?? "unknown"),
    schedulerConfigDigest: runtime.scheduler_config_digest ? String(runtime.scheduler_config_digest) : null,
    apiConfigDigest: runtime.api_config_digest ? String(runtime.api_config_digest) : null,
    resourcePools:
      runtime.resource_pools && typeof runtime.resource_pools === "object"
        ? runtime.resource_pools
        : {},
    metrics: runtime.metrics && typeof runtime.metrics === "object" ? runtime.metrics : {},
  };
}

const schedulerBlockedReasonLabels: Record<string, string> = {
  scheduler_paused: "调度器已暂停",
  config_disabled: "配置已禁用",
  retry_backoff: "等待重试退避",
  dependency_not_satisfied: "依赖任务未完成",
  outside_trading_session: "当前不在交易时段",
  recovery_domain_blocked: "数据补跑门控",
  mutex_busy: "互斥任务运行中",
  resource_pool_full: "资源池额度已满",
};

function schedulerDetailList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item ?? "").trim()).filter(Boolean)
    : [];
}

export function schedulerRuntimeTaskDetail(task: SchedulerRuntimeTask): string {
  if (task.status === "blocked") {
    const detail = task.blocked_detail && typeof task.blocked_detail === "object"
      ? task.blocked_detail
      : {};
    const parts: string[] = [];
    const reason = String(task.blocked_reason ?? "").trim();
    if (reason) {
      parts.push(schedulerBlockedReasonLabels[reason] ?? reason);
    }
    const domains = schedulerDetailList(detail.domains);
    if (domains.length > 0) {
      parts.push(`数据域 ${domains.join("、")}`);
    }
    const blockingTaskIds = schedulerDetailList(detail.blocking_task_ids);
    if (blockingTaskIds.length > 0) {
      parts.push(`依赖 ${blockingTaskIds.join("、")}`);
    }
    const mutexKey = String(detail.mutex_key ?? task.mutex_key ?? "").trim();
    if (mutexKey) {
      parts.push(`互斥键 ${mutexKey}`);
    }
    const resourcePool = String(detail.resource_pool ?? "").trim();
    if (resourcePool) {
      parts.push(`资源池 ${resourcePool} ${detail.running ?? "?"} / ${detail.limit ?? "?"}`);
    }
    if (task.blocked_until) {
      parts.push(`复查时间 ${task.blocked_until}`);
    }
    const recoveryBlockers = Array.isArray(detail.blockers)
      ? detail.blockers
          .filter((item: unknown) => item && typeof item === "object")
          .map((item: Record<string, unknown>) =>
            [item.run_id, item.step_id, item.target_id]
              .map((value) => String(value ?? "").trim())
              .filter(Boolean)
              .join(" / "),
          )
          .filter(Boolean)
      : [];
    if (recoveryBlockers.length > 0) {
      parts.push(`补跑阻塞 ${recoveryBlockers.join("、")}`);
    }
    return parts.join(" · ") || "等待阻塞条件解除";
  }
  if (task.status === "running") {
    const lease = task.lease_owner ? `租约 ${task.lease_owner}` : "租约未登记";
    const progress = Number(task.progress?.summary?.progress_ratio);
    return Number.isFinite(progress) ? `${lease} · ${Math.round(progress * 100)}%` : lease;
  }
  if (task.status === "failed") {
    return String(task.error_message ?? "执行失败，暂无错误摘要");
  }
  if (task.status === "pending") {
    return task.scheduled_for ? `计划 ${task.scheduled_for}` : "已准入，等待 worker 领取";
  }
  return String(task.updated_at ?? task.finished_at ?? task.scheduled_for ?? "-");
}

export function filterSchedulerRuntimeTasks(
  tasks: SchedulerRuntimeTask[],
  status: SchedulerRuntimeStatus | "all",
): SchedulerRuntimeTask[] {
  return status === "all" ? tasks : tasks.filter((task) => task.status === status);
}

export function summarizeProcessingPlan(preview: Record<string, any> | undefined): {
  normalizationLabel: string;
  analyticsLabel: string;
  stageCount: number;
  requiredInput: string;
} {
  const processing = preview?.processing ?? {};
  const stages = Array.isArray(processing.stages) ? processing.stages : [];
  return {
    normalizationLabel: processingStatusLabel(processing.normalization?.status),
    analyticsLabel: processingStatusLabel(
      processing.analytics?.scheduler_status ?? processing.analytics?.status,
    ),
    stageCount: stages.length,
    requiredInput: processing.analytics?.required_runtime_input ?? "universe_id",
  };
}
