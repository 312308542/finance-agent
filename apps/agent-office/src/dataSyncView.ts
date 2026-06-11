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

const marketOrder = ["ashare", "fund", "crypto_spot", "crypto_future"];
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

export function summarizeSchedulerStatus(status: Record<string, any> | null | undefined): SchedulerSummary {
  const health = status?.health ?? {};
  const process = status?.process ?? {};
  const running = Boolean(process.running);
  const lastJob = health.last_job ?? "暂无任务";
  const lastJobStatus = health.last_job_status ?? health.status ?? "unknown";
  const pid = process.pid ? `PID ${process.pid}` : "未启动进程";

  if (running) {
    return {
      tone: "green",
      label: "运行中",
      detail: `${pid} · ${lastJob} / ${lastJobStatus}`,
    };
  }
  if (health.status === "missing") {
    return {
      tone: "amber",
      label: "未启动",
      detail: "还没有调度器状态文件",
    };
  }
  if (health.healthy) {
    return {
      tone: "blue",
      label: "最近健康",
      detail: `${pid} · ${lastJob} / ${lastJobStatus}`,
    };
  }
  return {
    tone: "red",
    label: "需要处理",
    detail: `${pid} · ${lastJob} / ${lastJobStatus}`,
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
