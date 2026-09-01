import * as React from "react";
import { AlertTriangle, Check, CheckCircle2, CircleDot, Clock3, Database, ListChecks, Pause, Play, RefreshCcw, Search, ServerCog, Settings2, Wifi, X } from "lucide-react";
import { cancelDataSchedulerJob, pauseDataSchedulerJob, rerunFailedDataSchedulerJob, resumeDataSchedulerJob, runDataSchedulerJob, updateDataSchedulerJob } from "../api";
import type { ConsolePageProps } from "../consoleTypes";
import { MetricBlock } from "../components/consoleCommon";
import { buildTaskMonitorModel, estimateRemainingSeconds, extractFallbackWaitingTasks, extractSchedulerJobTasks, filterTaskMonitorItems, formatCompactNumber, formatDateTime, formatDuration, formatPercent, formatTaskLogLine, statusTone, taskMonitorFilters, taskProgressLabel, type TaskMonitorFilter, type TaskMonitorItem, type TaskMonitorSourceRateState } from "../taskMonitorView";

export function TaskMonitorPage({
  taskSchedulerProgress,
  taskSchedulerJobs,
  dataSyncConfig,
  dataSchedulerStatus,
  taskMonitorLoading,
  refreshTaskMonitor,
}: Pick<
  ConsolePageProps,
  | "taskSchedulerProgress"
  | "taskSchedulerJobs"
  | "dataSyncConfig"
  | "dataSchedulerStatus"
  | "taskMonitorLoading"
  | "refreshTaskMonitor"
>) {
  const fallbackWaiting = React.useMemo(
    () => [
      ...extractFallbackWaitingTasks(dataSyncConfig, dataSchedulerStatus),
      ...extractSchedulerJobTasks(taskSchedulerJobs),
    ],
    [dataSyncConfig, dataSchedulerStatus, taskSchedulerJobs],
  );
  const model = React.useMemo(
    () => buildTaskMonitorModel(taskSchedulerProgress, { fallbackWaiting }),
    [taskSchedulerProgress, fallbackWaiting],
  );
  const [statusFilter, setStatusFilter] = React.useState<TaskMonitorFilter>("all");
  const [query, setQuery] = React.useState("");
  const [selectedId, setSelectedId] = React.useState("");
  const [openConfigSignal, setOpenConfigSignal] = React.useState(0);
  const [listRunningJob, setListRunningJob] = React.useState("");
  const [listActionStatus, setListActionStatus] = React.useState<{
    tone: "green" | "amber" | "red" | "blue";
    message: string;
  } | null>(null);
  const filteredItems = React.useMemo(
    () => filterTaskMonitorItems(model.items, statusFilter, query),
    [model.items, statusFilter, query],
  );
  const selectedTask =
    filteredItems.find((item) => item.id === selectedId) ?? filteredItems[0] ?? model.items[0] ?? null;
  const connectionTone =
    model.connectionStatus === "ok" ? "green" : model.connectionStatus === "degraded" ? "amber" : "red";
  const connectionLabel =
    model.connectionStatus === "ok"
      ? "实时进度已连接"
      : model.connectionStatus === "degraded"
        ? "实时进度降级"
        : "等待进度接口";
  const counts: Record<TaskMonitorFilter, number> = {
    all: model.items.length,
    running: model.items.filter((item) => item.status === "running").length,
    paused: model.items.filter((item) => item.status === "paused").length,
    waiting: model.items.filter((item) => ["waiting", "blocked", "locked"].includes(item.status)).length,
    completed: model.items.filter((item) => item.status === "completed").length,
    failed: model.items.filter((item) => item.status === "failed").length,
  };

  React.useEffect(() => {
    if (!selectedTask) {
      setSelectedId("");
      return;
    }
    if (selectedTask.id !== selectedId) {
      setSelectedId(selectedTask.id);
    }
  }, [selectedTask, selectedId]);

  const runTaskCard = async (task: TaskMonitorItem) => {
    if (!task.jobName) {
      setListActionStatus({ tone: "red", message: "当前任务缺少调度任务 ID，无法执行。" });
      return;
    }
    setSelectedId(task.id);
    setListRunningJob(task.jobName);
    setListActionStatus({ tone: "blue", message: `正在启动 ${task.title} ...` });
    try {
      const result = await runDataSchedulerJob(task.jobName, { dry_run: false });
      setListActionStatus({
        tone: result.status === "ok" ? "green" : "red",
        message: String(result.message ?? (result.status === "ok" ? `${task.title} 已启动。` : `${task.title} 启动失败。`)),
      });
      await refreshTaskMonitor?.();
    } finally {
      setListRunningJob("");
    }
  };

  return (
    <section className="task-monitor-shell">
      <aside className="task-monitor-list-panel" aria-label="任务队列">
        <header className="task-monitor-list-head">
          <div className="task-monitor-list-title">
            <ListChecks size={18} />
            <h2>任务队列</h2>
          </div>
          <span className={`task-cache-dot tone-${connectionTone}`}>{model.cacheBackend || "cache"}</span>
        </header>

        <div className="task-monitor-filters" aria-label="任务状态筛选">
          {taskMonitorFilters.map((filter) => (
            <button
              key={filter.id}
              className={statusFilter === filter.id ? "task-filter-button is-active" : "task-filter-button"}
              onClick={() => setStatusFilter(filter.id)}
            >
              <span>{filter.label}</span>
              <em>{counts[filter.id]}</em>
            </button>
          ))}
        </div>

        <label className="task-monitor-search">
          <Search size={16} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索任务名称 / ID"
          />
        </label>

        {model.connectionStatus !== "ok" ? (
          <div className={`task-monitor-hint tone-${connectionTone}`}>
            {model.message || "后端进度接口尚未返回实时数据，当前展示等待队列或空态。"}
          </div>
        ) : null}
        {listActionStatus ? (
          <div className={`task-monitor-hint tone-${listActionStatus.tone}`}>
            {listActionStatus.message}
          </div>
        ) : null}

        <div className="task-list">
          {filteredItems.length > 0 ? (
            filteredItems.map((task) => (
              <article
                key={task.id}
                className={selectedTask?.id === task.id ? "task-list-item is-active" : "task-list-item"}
                role="button"
                tabIndex={0}
                onClick={() => setSelectedId(task.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setSelectedId(task.id);
                  }
                }}
              >
                <span className={`task-card-icon tone-${task.tone}`}>
                  {task.status === "completed" ? (
                    <CheckCircle2 size={21} />
                  ) : task.status === "failed" ? (
                    <AlertTriangle size={21} />
                  ) : task.status === "waiting" ? (
                    <Clock3 size={21} />
                  ) : task.status === "paused" ? (
                    <Pause size={21} />
                  ) : (
                    <RefreshCcw size={21} />
                  )}
                </span>
                <div className="task-card-main">
                  <div className="task-list-title">
                    <strong>{task.title}</strong>
                    <em>{taskProgressLabel(task)}</em>
                  </div>
                  {task.description ? (
                    <span
                      className="task-card-hover-text task-card-description"
                      data-tooltip={task.description}
                      aria-label={task.description}
                      tabIndex={0}
                    >
                      <span>{task.description}</span>
                    </span>
                  ) : null}
                  <span
                    className="task-card-hover-text"
                    data-tooltip={`任务 ID：${task.jobName || "未命名 job"}`}
                    aria-label={`任务 ID：${task.jobName || "未命名 job"}`}
                    tabIndex={0}
                  >
                    <span>任务 ID：{task.jobName || "未命名 job"}</span>
                  </span>
                  <div className="task-list-progress">
                    <TaskProgressBar
                      ratio={task.progressRatio}
                      tone={task.tone}
                      indeterminate={!task.progressAvailable && task.status === "running"}
                    />
                  </div>
                </div>
                <div className="task-card-actions">
                  <button
                    className="task-card-action-button"
                    type="button"
                    disabled={!task.jobName || listRunningJob === task.jobName}
                    title={`立即执行 ${task.title}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      void runTaskCard(task);
                    }}
                  >
                    <CircleDot size={14} />
                    <span>{listRunningJob === task.jobName ? "启动中" : "执行"}</span>
                  </button>
                  <button
                    className="task-card-action-button"
                    type="button"
                    disabled={!task.jobName}
                    title={`配置 ${task.title}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      setSelectedId(task.id);
                      setOpenConfigSignal((signal) => signal + 1);
                    }}
                  >
                    <Settings2 size={14} />
                    <span>配置</span>
                  </button>
                </div>
                <div className="task-card-meta">
                  <small className={`task-state-text tone-${task.tone}`}>{task.statusLabel}</small>
                  <small>开始于 {formatDateTime(task.startedAt) || "--"}</small>
                  <small>
                    {task.status === "waiting"
                      ? "等待队列"
                      : `剩余 ${
                          estimateRemainingSeconds(
                            task.metrics?.remaining_seconds,
                            task.summary.completedItems,
                            task.summary.remainingItems,
                            task.metrics?.duration_seconds,
                          ) === null
                            ? "--"
                            : formatDuration(
                                estimateRemainingSeconds(
                                  task.metrics?.remaining_seconds,
                                  task.summary.completedItems,
                                  task.summary.remainingItems,
                                  task.metrics?.duration_seconds,
                                ),
                              )
                        }`}
                  </small>
                </div>
              </article>
            ))
          ) : (
            <div className="task-monitor-empty">
              <ServerCog size={24} />
              <strong>{taskMonitorLoading ? "正在读取任务进度" : "没有符合筛选条件的任务"}</strong>
              <span>{connectionLabel}</span>
            </div>
          )}
        </div>
      </aside>

      <TaskDetailView
        task={selectedTask}
        model={model}
        schedulerJobs={taskSchedulerJobs}
        openConfigSignal={openConfigSignal}
        loading={Boolean(taskMonitorLoading)}
        refreshTaskMonitor={refreshTaskMonitor}
      />
    </section>
  );
}

type TaskConfigDraft = {
  enabled: boolean;
  intervalSeconds: string;
  limit: string;
  batchSize: string;
  maxWorkers: string;
  scheduleType: string;
  runAt: string;
  timezone: string;
  tradingDayPolicy: string;
};

function buildTaskConfigDraft(job: any, task: TaskMonitorItem | null): TaskConfigDraft {
  const params = job?.params && typeof job.params === "object" ? job.params : {};
  const runAt = Array.isArray(job?.run_at) ? job.run_at.join(", ") : "";
  return {
    enabled: Boolean(job?.enabled ?? true),
    intervalSeconds: draftString(job?.interval_seconds, task?.intervalSeconds),
    limit: draftString(job?.limit),
    batchSize: draftString(params.batch_size, task?.batchSize),
    maxWorkers: draftString(params.max_workers, task?.maxWorkers),
    scheduleType: String(job?.schedule_type ?? "interval"),
    runAt,
    timezone: String(job?.timezone ?? "Asia/Shanghai"),
    tradingDayPolicy: String(job?.trading_day_policy ?? "any_day"),
  };
}

function buildTaskConfigPayload(draft: TaskConfigDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    interval_seconds: optionalInteger(draft.intervalSeconds, 0),
    limit: optionalInteger(draft.limit, 1),
    batch_size: optionalInteger(draft.batchSize, 1),
    max_workers: optionalInteger(draft.maxWorkers, 1),
    schedule_type: draft.scheduleType.trim() || null,
    run_at: draft.runAt
      .split(/[,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean),
    timezone: draft.timezone.trim() || null,
    trading_day_policy: draft.tradingDayPolicy.trim() || null,
  };
}

function draftString(...values: unknown[]): string {
  const value = values.find((item) => item !== undefined && item !== null && item !== "");
  return value === undefined ? "" : String(value);
}

function optionalInteger(value: string, min: number): number | null {
  const cleanValue = value.trim();
  if (!cleanValue) {
    return null;
  }
  const numericValue = Number(cleanValue);
  if (!Number.isFinite(numericValue)) {
    return null;
  }
  return Math.max(min, Math.round(numericValue));
}

function TaskDetailView({
  task,
  model,
  schedulerJobs,
  openConfigSignal,
  loading,
  refreshTaskMonitor,
}: {
  task: TaskMonitorItem | null;
  model: ReturnType<typeof buildTaskMonitorModel>;
  schedulerJobs?: Record<string, any> | null;
  openConfigSignal?: number;
  loading: boolean;
  refreshTaskMonitor?: (silent?: boolean) => Promise<void>;
}) {
  const [logFilter, setLogFilter] = React.useState<"all" | "error">("all");
  const [logDetail, setLogDetail] = React.useState<{ key: string; content: string } | null>(null);
  const [configOpen, setConfigOpen] = React.useState(false);
  const [configSaving, setConfigSaving] = React.useState(false);
  const [runningAction, setRunningAction] = React.useState<"run" | "dry-run" | "rerun-failed" | "pause" | "resume" | "cancel" | null>(
    null,
  );
  const [taskActionStatus, setTaskActionStatus] = React.useState<{
    tone: "green" | "amber" | "red" | "blue";
    message: string;
  } | null>(null);
  const schedulerJobList = React.useMemo(
    () => (Array.isArray(schedulerJobs?.data?.jobs) ? schedulerJobs?.data?.jobs : []),
    [schedulerJobs],
  );
  const schedulerJob = React.useMemo(
    () => schedulerJobList.find((job: any) => String(job?.name ?? "") === String(task?.jobName ?? "")) ?? null,
    [schedulerJobList, task?.jobName],
  );
  const [configDraft, setConfigDraft] = React.useState(() => buildTaskConfigDraft(null, null));

  React.useEffect(() => {
    setLogDetail(null);
  }, [task?.id, logFilter]);

  React.useEffect(() => {
    if (!configOpen) {
      setConfigDraft(buildTaskConfigDraft(schedulerJob, task));
    }
  }, [configOpen, schedulerJob, task]);

  React.useEffect(() => {
    setConfigOpen(false);
    setTaskActionStatus(null);
  }, [task?.id]);

  React.useEffect(() => {
    if (openConfigSignal) {
      setConfigOpen(true);
    }
  }, [openConfigSignal]);

  if (!task) {
    return (
      <section className="task-detail-panel task-detail-empty">
        <ServerCog size={30} />
        <strong>{loading ? "正在读取任务进度" : "暂无任务进度"}</strong>
        <span>
          {model.connectionStatus === "ok"
            ? "Redis 中暂时没有任务快照。"
            : "等待后端 progress 接口或 Redis 恢复后自动刷新。"}
        </span>
      </section>
    );
  }
  const durationSeconds = task.metrics?.duration_seconds ?? 0;
  const remainingSeconds = estimateRemainingSeconds(
    task.metrics?.remaining_seconds,
    task.summary.completedItems,
    task.summary.remainingItems,
    durationSeconds,
  );
  const nodeValue = task.metrics?.node ?? "worker-01";
  const backendValue = task.metrics?.cache_backend ?? model.cacheBackend ?? "-";
  const completedText = `${formatCompactNumber(task.summary.completedItems)} / ${formatCompactNumber(
    task.summary.totalItems,
  )}`;
  const sourceRateStates = selectSourceRateStates(task, model.sourceRateStates);
  const workerValue = schedulerJob?.params?.max_workers || task.maxWorkers || task.metrics?.max_workers || "-";
  const effectiveSourceConcurrency = formatEffectiveSourceConcurrency(sourceRateStates);
  const globalConcurrencyValue =
    model.globalConcurrency.limit > 0
      ? `${formatCompactNumber(model.globalConcurrency.running)} / ${formatCompactNumber(model.globalConcurrency.limit)}`
      : "-";
  const resourcePoolState = model.resourcePools[task.resourcePool];
  const resourcePoolValue = formatResourcePoolRuntime(task.resourcePool, resourcePoolState);
  const taskPriorityValue = Number.isFinite(task.priority) ? String(task.priority) : "-";
  const abnormalEvents = task.events.filter(
    (event) =>
      event.status === "failed" ||
      event.eventType.includes("failed") ||
      Boolean(event.errorMessage) ||
      event.retryCount > 0 ||
      event.retryAfterSeconds > 0 ||
      Boolean(event.nextRetryAt),
  );
  const filteredEvents = logFilter === "error" ? abnormalEvents : task.events;
  const canOperateTask = Boolean(task.jobName);
  const rerunFailedDisabledReason = !canOperateTask
    ? "当前任务缺少调度任务 ID"
    : task.status === "running" || task.status === "paused"
      ? "任务结束后可重跑失败项"
    : task.summary.failedItems <= 0
      ? "当前任务没有失败项"
      : runningAction !== null
        ? "已有任务操作正在启动"
        : loading
          ? "正在刷新任务进度"
          : "";
  const canRerunFailedTask = !rerunFailedDisabledReason;
  const pauseDisabledReason = !canOperateTask
    ? "当前任务缺少调度任务 ID"
    : task.status !== "running"
      ? "只有运行中的任务可以暂停"
      : runningAction !== null
        ? "已有任务操作正在执行"
        : loading
          ? "正在刷新任务进度"
          : "";
  const canPauseTask = !pauseDisabledReason;
  const resumeDisabledReason = !canOperateTask
    ? "当前任务缺少调度任务 ID"
    : task.status !== "paused"
      ? "只有已暂停的任务可以继续"
      : runningAction !== null
        ? "已有任务操作正在执行"
        : loading
          ? "正在刷新任务进度"
          : "";
  const canResumeTask = !resumeDisabledReason;
  const cancelDisabledReason = !canOperateTask
    ? "当前任务缺少调度任务 ID"
    : task.status !== "running" && task.status !== "paused"
      ? "只有运行中或已暂停的任务可以取消"
      : runningAction !== null
        ? "已有任务操作正在执行"
        : loading
          ? "正在刷新任务进度"
          : "";
  const canCancelTask = !cancelDisabledReason;
  const updateDraft = (field: keyof TaskConfigDraft, value: string | boolean) => {
    setConfigDraft((draft) => ({ ...draft, [field]: value }));
  };
  const runSelectedTask = async (dryRun: boolean) => {
    if (!task.jobName) {
      setTaskActionStatus({ tone: "red", message: "当前任务缺少调度任务 ID，无法执行。" });
      return;
    }
    const action = dryRun ? "dry-run" : "run";
    setRunningAction(action);
    setTaskActionStatus({
      tone: "blue",
      message: dryRun ? "正在启动单任务预演..." : "正在启动单任务真实执行...",
    });
    try {
      const result = await runDataSchedulerJob(task.jobName, { dry_run: dryRun });
      setTaskActionStatus({
        tone: result.status === "ok" ? "green" : "red",
        message: String(result.message ?? (result.status === "ok" ? "任务已启动。" : "任务启动失败。")),
      });
      await refreshTaskMonitor?.();
    } finally {
      setRunningAction(null);
    }
  };
  const rerunFailedSelectedTask = async () => {
    if (!task.jobName) {
      setTaskActionStatus({ tone: "red", message: "当前任务缺少调度任务 ID，无法重跑失败项。" });
      return;
    }
    if (task.status === "running" || task.status === "paused") {
      setTaskActionStatus({ tone: "amber", message: "任务尚未结束，结束后才能重跑失败项。" });
      return;
    }
    if (task.summary.failedItems <= 0) {
      setTaskActionStatus({ tone: "amber", message: "当前任务没有失败项，不需要重跑。" });
      return;
    }
    setRunningAction("rerun-failed");
    setTaskActionStatus({ tone: "blue", message: "正在把失败项重跑加入后台串行队列..." });
    try {
      const result = await rerunFailedDataSchedulerJob(task.jobName, { dry_run: false });
      setTaskActionStatus({
        tone: result.status === "ok" ? "green" : "red",
        message: String(result.message ?? (result.status === "ok" ? "失败项重跑已入队。" : "失败项重跑入队失败。")),
      });
      await refreshTaskMonitor?.();
    } finally {
      setRunningAction(null);
    }
  };
  const pauseSelectedTask = async () => {
    if (!task.jobName) {
      setTaskActionStatus({ tone: "red", message: "当前任务缺少调度任务 ID，无法暂停。" });
      return;
    }
    if (task.status !== "running") {
      setTaskActionStatus({ tone: "amber", message: "只有运行中的任务可以暂停。" });
      return;
    }
    setRunningAction("pause");
    setTaskActionStatus({ tone: "blue", message: "正在暂停当前任务..." });
    try {
      const result = await pauseDataSchedulerJob(task.jobName);
      setTaskActionStatus({
        tone: result.status === "ok" ? "green" : "red",
        message: String(result.message ?? (result.status === "ok" ? "任务暂停请求已提交。" : "任务暂停失败。")),
      });
      await refreshTaskMonitor?.();
    } finally {
      setRunningAction(null);
    }
  };
  const resumeSelectedTask = async () => {
    if (!task.jobName) {
      setTaskActionStatus({ tone: "red", message: "当前任务缺少调度任务 ID，无法继续。" });
      return;
    }
    if (task.status !== "paused") {
      setTaskActionStatus({ tone: "amber", message: "只有已暂停的任务可以继续。" });
      return;
    }
    setRunningAction("resume");
    setTaskActionStatus({ tone: "blue", message: "正在继续当前任务..." });
    try {
      const result = await resumeDataSchedulerJob(task.jobName);
      setTaskActionStatus({
        tone: result.status === "ok" ? "green" : "red",
        message: String(result.message ?? (result.status === "ok" ? "任务继续请求已提交。" : "任务继续失败。")),
      });
      await refreshTaskMonitor?.();
    } finally {
      setRunningAction(null);
    }
  };
  const cancelSelectedTask = async () => {
    if (!task.jobName) {
      setTaskActionStatus({ tone: "red", message: "当前任务缺少调度任务 ID，无法取消。" });
      return;
    }
    if (task.status !== "running" && task.status !== "paused") {
      setTaskActionStatus({ tone: "amber", message: "只有运行中或已暂停的任务可以取消。" });
      return;
    }
    setRunningAction("cancel");
    setTaskActionStatus({ tone: "blue", message: "正在取消当前任务..." });
    try {
      const result = await cancelDataSchedulerJob(task.jobName);
      setTaskActionStatus({
        tone: result.status === "ok" ? "green" : "red",
        message: String(result.message ?? (result.status === "ok" ? "任务取消请求已提交。" : "任务取消失败。")),
      });
      await refreshTaskMonitor?.();
    } finally {
      setRunningAction(null);
    }
  };
  const saveTaskConfig = async (runAfterSave = false) => {
    if (!task.jobName) {
      setTaskActionStatus({ tone: "red", message: "当前任务缺少调度任务 ID，无法保存配置。" });
      return;
    }
    setConfigSaving(true);
    setTaskActionStatus({
      tone: "blue",
      message:
        runAfterSave && (task.status === "running" || task.status === "paused")
          ? "正在保存任务配置，当前任务会在下一只标的提交前读取新配置..."
          : runAfterSave
            ? "正在保存任务配置并启动执行..."
            : "正在保存任务配置...",
    });
    try {
      const result = await updateDataSchedulerJob(task.jobName, buildTaskConfigPayload(configDraft));
      if (result.status !== "ok") {
        setTaskActionStatus({
          tone: "red",
          message: String(result.message ?? "任务配置保存失败。"),
        });
        await refreshTaskMonitor?.();
        return;
      }
      setTaskActionStatus({
        tone: "green",
        message: String(result.message ?? "任务配置已保存。"),
      });
      if (!runAfterSave) {
        setConfigOpen(false);
        await refreshTaskMonitor?.();
        return;
      }
      if (task.status === "running" || task.status === "paused") {
        const runningJobParams =
          schedulerJob?.params && typeof schedulerJob.params === "object" ? schedulerJob.params : {};
        const runningSyncTaskType = String(runningJobParams.sync_task_type ?? task.taskType ?? "");
        const runningIsFullHistoryMarketBarsTask = runningSyncTaskType === "market_bars_full_history_backfill";
        setTaskActionStatus({
          tone: "green",
          message: runningIsFullHistoryMarketBarsTask
            ? "配置已保存；新提交的标的会读取最新配置，已在请求中的标的不会被中断。10 年历史 K 线按日期范围拉取，不受条数上限截断。"
            : "配置已保存；新提交的标的会读取最新条数上限、批次大小和并发数，已在请求中的标的不会被中断。",
        });
        setConfigOpen(false);
        await refreshTaskMonitor?.();
        return;
      }
      setRunningAction("run");
      const runResult = await runDataSchedulerJob(task.jobName, { dry_run: false });
      setTaskActionStatus({
        tone: runResult.status === "ok" ? "green" : "red",
        message: String(runResult.message ?? (runResult.status === "ok" ? "配置已保存，任务已启动。" : "配置已保存，但任务启动失败。")),
      });
      if (runResult.status === "ok") {
        setConfigOpen(false);
      }
      await refreshTaskMonitor?.();
    } finally {
      setConfigSaving(false);
      if (runAfterSave) {
        setRunningAction(null);
      }
    }
  };
  const taskCanHotReload = task.status === "running" || task.status === "paused";
  const schedulerParams = schedulerJob?.params && typeof schedulerJob.params === "object" ? schedulerJob.params : {};
  const syncTaskType = String(schedulerParams.sync_task_type ?? task.taskType ?? "");
  const isMarketBarsTask = syncTaskType.startsWith("market_bars") || task.jobName.includes(".bars.");
  const isFullHistoryMarketBarsTask = syncTaskType === "market_bars_full_history_backfill";
  const limitFieldLabel = isMarketBarsTask ? "单标的条数上限" : "列表条数上限";
  const limitFieldHelp = isMarketBarsTask
    ? "控制非初始化 K 线任务每只标的最多拉取的 K 线条数；10 年历史初始化任务不使用该上限。"
    : "控制列表型数据源的临时拉取数量；留空表示使用任务默认值。";
  const saveAndRunLabel = taskCanHotReload ? "保存并热生效" : "保存并执行";
  const saveAndRunTitle =
    taskCanHotReload
      ? "保存当前任务配置；长任务会在下一只标的提交前读取新配置"
      : "保存当前任务配置并立即启动一次真实执行";
  return (
    <section className="task-detail-panel">
      <header className="task-detail-head">
        <div className="task-title-block">
          <div className="task-title-row">
            <span className="task-detail-icon">
              <ServerCog size={22} />
            </span>
            <h2>{task.title}</h2>
            <TaskStatusPill task={task} />
          </div>
          <div className="task-meta-row">
            <span>任务 ID：{task.jobName || "未命名 job"}</span>
            <span>开始于 {formatDateTime(task.startedAt) || "--"}</span>
            <span>{task.isRealtime ? "实时快照" : "等待队列"}</span>
            <span>资源池：{task.resourcePool || "default"}</span>
            <span>优先级 {taskPriorityValue}</span>
          </div>
          {task.description ? <p className="task-detail-description">{task.description}</p> : null}
        </div>
        <div className="task-head-actions">
          <button
            className="button button-primary"
            onClick={() => void runSelectedTask(false)}
            disabled={!canOperateTask || loading || runningAction !== null}
            title="立即以真实写库模式执行当前任务"
          >
            <CircleDot size={16} />
            {runningAction === "run" ? "启动中" : "立即执行"}
          </button>
          <button
            className="button button-ghost"
            onClick={() => void runSelectedTask(true)}
            disabled={!canOperateTask || loading || runningAction !== null}
            title="以 dry-run 模式预演当前任务，不写入业务数据"
          >
            <Check size={16} />
            {runningAction === "dry-run" ? "预演中" : "预演一次"}
          </button>
          <button
            className="button button-ghost"
            onClick={() => void rerunFailedSelectedTask()}
            disabled={!canRerunFailedTask}
            title={rerunFailedDisabledReason || "将失败项移交后台串行队列重跑"}
          >
            <RefreshCcw size={16} />
            {runningAction === "rerun-failed" ? "入队中" : "重跑失败项"}
          </button>
          <button
            className="button button-ghost"
            onClick={() => void pauseSelectedTask()}
            disabled={!canPauseTask}
            title={pauseDisabledReason || "暂停当前任务；正在请求的标的会先完成"}
          >
            <Pause size={16} />
            {runningAction === "pause" ? "暂停中" : "暂停"}
          </button>
          <button
            className="button button-ghost"
            onClick={() => void resumeSelectedTask()}
            disabled={!canResumeTask}
            title={resumeDisabledReason || "从当前断点继续提交后续标的"}
          >
            <Play size={16} />
            {runningAction === "resume" ? "继续中" : "继续"}
          </button>
          <button
            className="button button-ghost"
            onClick={() => setConfigOpen((open) => !open)}
            disabled={!canOperateTask}
            title="编辑当前调度任务的运行时配置"
          >
            <Settings2 size={16} />
            编辑配置
          </button>
          <button
            className="button button-ghost"
            onClick={() => void refreshTaskMonitor?.()}
            disabled={loading}
            title="立即刷新任务进度"
          >
            <RefreshCcw size={16} />
            {loading ? "刷新中" : "刷新"}
          </button>
          <button
            className="button button-danger"
            onClick={() => void cancelSelectedTask()}
            disabled={!canCancelTask}
            title={cancelDisabledReason || "取消当前正在运行的单任务"}
          >
            <X size={16} />
            {runningAction === "cancel" ? "取消中" : "取消任务"}
          </button>
        </div>
      </header>

      {taskActionStatus ? (
        <div className={`notice task-action-notice tone-${taskActionStatus.tone}`}>
          {taskActionStatus.message}
        </div>
      ) : null}

      {configOpen ? (
        <section className="task-config-drawer" aria-label="任务配置">
          <div className="task-section-title">
            <strong>任务配置</strong>
            <span>{schedulerJob ? "运行时调度配置" : "未读取到配置快照"}</span>
          </div>
          <div className="task-config-grid">
            <label className="task-config-check">
              <input
                type="checkbox"
                checked={configDraft.enabled}
                onChange={(event) => updateDraft("enabled", event.target.checked)}
              />
              <span>启用任务</span>
            </label>
            <label>
              <span>执行模式</span>
              <select
                value={configDraft.scheduleType}
                onChange={(event) => updateDraft("scheduleType", event.target.value)}
              >
                <option value="interval">按间隔执行</option>
                <option value="daily_time">按固定时间执行</option>
                <option value="trading_session">按交易时段执行</option>
                <option value="after_success">依赖成功后执行</option>
                <option value="manual">仅手动执行</option>
              </select>
            </label>
            <label>
              <span>间隔秒数</span>
              <input
                type="number"
                min="0"
                value={configDraft.intervalSeconds}
                onChange={(event) => updateDraft("intervalSeconds", event.target.value)}
              />
            </label>
            {isFullHistoryMarketBarsTask ? (
              <label>
                <span>K 线条数上限</span>
                <input type="text" value="不限制" disabled aria-label="K 线条数上限" />
                <small>10 年历史日 K 按日期范围拉取，不受条数上限截断；批次大小只控制每批提交多少只股票。</small>
              </label>
            ) : (
              <label>
                <span>{limitFieldLabel}</span>
                <input
                  type="number"
                  min="1"
                  value={configDraft.limit}
                  onChange={(event) => updateDraft("limit", event.target.value)}
                />
                <small>{limitFieldHelp}</small>
              </label>
            )}
            <label>
              <span>批次大小</span>
              <input
                type="number"
                min="1"
                value={configDraft.batchSize}
                onChange={(event) => updateDraft("batchSize", event.target.value)}
              />
            </label>
            <label>
              <span>任务并发数</span>
              <input
                type="number"
                min="1"
                value={configDraft.maxWorkers}
                onChange={(event) => updateDraft("maxWorkers", event.target.value)}
              />
              <small>控制单个任务内部同时处理的标的数量，数据源退避仍可能临时降速。</small>
            </label>
            <label>
              <span>固定时间</span>
              <input
                value={configDraft.runAt}
                onChange={(event) => updateDraft("runAt", event.target.value)}
                placeholder="例如 12:05, 15:10"
              />
            </label>
            <label>
              <span>时区</span>
              <input
                value={configDraft.timezone}
                onChange={(event) => updateDraft("timezone", event.target.value)}
                placeholder="Asia/Shanghai"
              />
            </label>
            <label>
              <span>交易日策略</span>
              <select
                value={configDraft.tradingDayPolicy}
                onChange={(event) => updateDraft("tradingDayPolicy", event.target.value)}
              >
                <option value="any_day">任意日期</option>
                <option value="trading_day_only">仅交易日</option>
                <option value="non_trading_day_only">仅非交易日</option>
                <option value="previous_trading_day_required">要求前一交易日</option>
              </select>
            </label>
          </div>
          <div className="task-config-actions">
            <button className="button button-ghost" type="button" onClick={() => setConfigOpen(false)}>
              关闭
            </button>
            <button
              className="button button-ghost"
              type="button"
              onClick={() => void saveTaskConfig(true)}
              disabled={configSaving || runningAction !== null || !canOperateTask}
              title={saveAndRunTitle}
            >
              <CircleDot size={16} />
              {configSaving ? "保存中" : saveAndRunLabel}
            </button>
            <button className="button button-primary" type="button" onClick={() => void saveTaskConfig()} disabled={configSaving}>
              <Check size={16} />
              {configSaving ? "保存中" : "保存配置"}
            </button>
          </div>
        </section>
      ) : null}

      {task.errorMessage ? <div className="notice notice-red">{task.errorMessage}</div> : null}

      <section className="task-metric-grid">
        <TaskMonitorMetric
          tone="blue"
          icon={<Database size={28} />}
          label="总同步数量"
          value={formatCompactNumber(task.summary.totalItems)}
          caption="需要同步的标的总数"
        />
        <TaskMonitorMetric
          tone="green"
          icon={<CheckCircle2 size={28} />}
          label="已完成"
          value={formatCompactNumber(task.summary.completedItems)}
          caption="完成同步"
        />
        <TaskMonitorMetric
          tone="amber"
          icon={<Clock3 size={28} />}
          label="处理中"
          value={formatCompactNumber(task.summary.runningItems)}
          caption="正在处理"
        />
        <TaskMonitorMetric
          tone="purple"
          icon={<CircleDot size={28} />}
          label="剩余"
          value={formatCompactNumber(task.summary.remainingItems)}
          caption="等待处理"
        />
        <TaskMonitorMetric
          tone="red"
          icon={<AlertTriangle size={28} />}
          label="失败"
          value={formatCompactNumber(task.summary.failedItems)}
          caption="需排查或等待重试"
        />
        <TaskMonitorMetric
          tone="amber"
          icon={<RefreshCcw size={28} />}
          label="待重试"
          value={formatCompactNumber(task.summary.retryItems)}
          caption="已有下次重试时间"
        />
      </section>

      <section className="task-overall-card">
        <div className="task-section-title">
          <strong>整体进度</strong>
          <span>{taskProgressLabel(task)}</span>
        </div>
        <TaskProgressBar
          ratio={task.progressRatio}
          tone="blue"
          indeterminate={!task.progressAvailable && task.status === "running"}
        />
        <div className="task-progress-meta">
          <span>{completedText} 已完成</span>
          <span>预计剩余时间 {remainingSeconds === null ? "--" : formatDuration(remainingSeconds)}</span>
        </div>
      </section>

      <section className="task-source-rate-card">
        <div className="task-section-title">
          <strong>数据源限频与退避</strong>
          <span>{sourceRateStates.length ? `${sourceRateStates.length} 个源` : "暂无运行态"}</span>
        </div>
        {sourceRateStates.length > 0 ? (
          <div className="source-rate-list">
            {sourceRateStates.map((state) => {
              const tone = sourceRateTone(state);
              const isEastmoneyCookie = state.sourceKey === "eastmoney_kline_cookie";
              return (
                <article key={state.sourceKey} className={`source-rate-item tone-${tone}`}>
                  <span className="source-rate-icon">
                    {tone === "red" ? <AlertTriangle size={18} /> : <Wifi size={18} />}
                  </span>
                  <div className="source-rate-main">
                    <div className="source-rate-head">
                      <strong>{sourceRateTitle(state)}</strong>
                      <em>{sourceRateLabel(state)}</em>
                    </div>
                    {isEastmoneyCookie ? (
                      <>
                        <div className="source-rate-metrics">
                          <span>冷却 {state.cooldownRemainingSeconds > 0 ? formatDuration(state.cooldownRemainingSeconds) : "-"}</span>
                          <span>探测 {state.probeOk ? "通过" : state.state === "healthy" ? "通过" : "待验证"}</span>
                          <span>有效 {state.effectiveMaxConcurrency > 0 ? "是" : "否"}</span>
                        </div>
                        <div className="source-rate-footer">
                          <span title={state.lastErrorMessage || "暂无错误"}>{state.lastErrorMessage ? `上次错误 ${state.lastErrorMessage}` : "暂无 Cookie 校验错误"}</span>
                        </div>
                      </>
                    ) : (
                      <>
                        <div className="source-rate-metrics">
                          <span>源有效并发 {state.effectiveMaxConcurrency || "-"}</span>
                          <span>间隔 {formatRateInterval(state.effectiveMinIntervalSeconds)}</span>
                          <span>失败率 {formatPercent(state.failureRate)}</span>
                          <span>异常 {formatCompactNumber(sourceRateErrorCount(state))}</span>
                        </div>
                        <div className="source-rate-footer">
                          <span>超时 {formatCompactNumber(state.timeoutCount)}</span>
                          <span>断连 {formatCompactNumber(state.disconnectCount)}</span>
                          <span>限流 {formatCompactNumber(state.rateLimitedCount)}</span>
                          <span>恢复 {formatDateTime(state.nextRecoverAt)}</span>
                        </div>
                      </>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="task-monitor-empty compact">
            <Wifi size={20} />
            <strong>暂无数据源退避状态</strong>
            <span>任务运行后会从 Redis 展示每个上游源的并发、间隔、失败率和冷却时间</span>
          </div>
        )}
      </section>

      <section className="task-detail-grid">
        <div className="task-section-block task-stage-panel">
          <div className="task-section-title">
            <strong>进度详情</strong>
            <span>精细到每个阶段</span>
          </div>
          <div className="stage-list">
            {task.stages.length > 0 ? (
              task.stages.map((stage) => (
                <article key={stage.key || stage.title} className="stage-row">
                  <span className={`stage-status-dot tone-${stage.tone}`}>{stage.status === "completed" ? <Check size={14} /> : null}</span>
                  <div className="stage-content">
                    <div className="stage-row-head">
                      <strong>{stage.title}</strong>
                      <em>{formatPercent(stage.progressRatio)}</em>
                    </div>
                    <TaskProgressBar ratio={stage.progressRatio} tone={stage.tone} />
                    <div className="stage-row-meta">
                      <span>{stage.statusLabel}</span>
                      <span>
                        {formatCompactNumber(stage.completedItems)} / {formatCompactNumber(stage.totalItems)}
                        {stage.failedItems ? ` · 失败 ${stage.failedItems}` : ""}
                      </span>
                    </div>
                  </div>
                </article>
              ))
            ) : (
              <div className="task-monitor-empty compact">
                <ServerCog size={20} />
                <strong>暂无阶段明细</strong>
                <span>等待采集任务写入阶段进度</span>
              </div>
            )}
          </div>
        </div>

        <div className="task-section-block task-log-block">
          <div className="task-section-title">
            <strong>实时日志</strong>
            <span>{logFilter === "error" ? `${filteredEvents.length} / ${task.events.length}` : task.events.length} 条</span>
          </div>
          <div className="task-log-toolbar">
            <button
              className={logFilter === "all" ? "task-log-filter is-active" : "task-log-filter"}
              type="button"
              onClick={() => setLogFilter("all")}
            >
              全部
            </button>
            <button
              className={logFilter === "error" ? "task-log-filter is-active" : "task-log-filter"}
              type="button"
              onClick={() => setLogFilter("error")}
            >
              只看异常
            </button>
          </div>
          {logDetail ? (
            <div className="task-log-popover" role="dialog" aria-label="完整日志内容">
              <div className="task-log-popover-head">
                <strong>完整日志</strong>
                <button type="button" onClick={() => setLogDetail(null)} aria-label="关闭完整日志">
                  <X size={14} />
                </button>
              </div>
              <pre>{logDetail.content}</pre>
            </div>
          ) : null}
          <div className="task-log-list">
            {filteredEvents.length > 0 ? (
              filteredEvents.map((event, index) => {
                const eventKey = `${event.createdAt}-${event.eventType}-${index}`;
                const logLine = formatTaskLogLine(event);
                const selected = logDetail?.key === eventKey;
                return (
                  <article
                    key={eventKey}
                    className={selected ? `tone-${statusTone(event.status)} is-selected` : `tone-${statusTone(event.status)}`}
                    role="button"
                    tabIndex={0}
                    title={logLine}
                    aria-label="查看完整日志"
                    onClick={() => setLogDetail(selected ? null : { key: eventKey, content: logLine })}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setLogDetail(selected ? null : { key: eventKey, content: logLine });
                      }
                    }}
                  >
                    <time>{formatDateTime(event.createdAt).split(" ").pop() || "--"}</time>
                    <CircleDot size={10} />
                    <span>{logLine}</span>
                  </article>
                );
              })
            ) : (
              <div className="task-monitor-empty compact">
                <CircleDot size={20} />
                <strong>{logFilter === "error" ? "暂无异常日志" : "暂无实时日志"}</strong>
                <span>{logFilter === "error" ? "当前任务暂未记录失败、错误或重试事件" : "单标的完成、失败和重试事件会显示在这里"}</span>
              </div>
            )}
          </div>
        </div>
      </section>

      <footer className="task-runtime-grid">
        <MetricBlock label="执行节点" value={nodeValue} />
        <MetricBlock label="全局任务并发" value={globalConcurrencyValue} />
        <MetricBlock label="资源池占用" value={resourcePoolValue} />
        <MetricBlock label="任务 worker" value={workerValue} />
        <MetricBlock label="源有效并发" value={effectiveSourceConcurrency} />
        <MetricBlock label="任务优先级" value={taskPriorityValue} />
        <MetricBlock label="吞吐量" value={`${formatCompactNumber(task.throughputPerMinute)} 条/分钟`} />
        <MetricBlock label="运行时长" value={formatDuration(durationSeconds)} />
        <MetricBlock label="错误数" value={task.summary.failedItems} />
        <MetricBlock label="缓存" value={backendValue} />
      </footer>
    </section>
  );
}

function selectSourceRateStates(
  task: TaskMonitorItem,
  states: TaskMonitorSourceRateState[],
): TaskMonitorSourceRateState[] {
  const providerHints = new Set(
    task.events
      .map((event) => event.providerKey)
      .filter(Boolean)
      .flatMap((providerKey) => [providerKey, providerKey.split(":").pop() ?? providerKey]),
  );
  const matched = states.filter((state) =>
    [...providerHints].some(
      (providerKey) => state.sourceKey === providerKey || providerKey.includes(state.sourceKey),
    ),
  );
  return (matched.length ? matched : states).slice(0, 4);
}

function formatEffectiveSourceConcurrency(states: TaskMonitorSourceRateState[]): string {
  const values = states
    .map((state) => state.effectiveMaxConcurrency)
    .filter((value) => Number.isFinite(value) && value > 0);
  if (!values.length) {
    return "-";
  }
  return [...new Set(values)].join(" / ");
}

function formatResourcePoolRuntime(
  poolName: string,
  state: { running: number; queued: number; limit: number } | undefined,
): string {
  const name = poolName || "default";
  if (!state) {
    return name;
  }
  const queued = state.queued > 0 ? `，排队 ${formatCompactNumber(state.queued)}` : "";
  return `${name} ${formatCompactNumber(state.running)} / ${formatCompactNumber(state.limit)}${queued}`;
}

function sourceRateErrorCount(state: TaskMonitorSourceRateState): number {
  return state.failureCount + state.timeoutCount + state.disconnectCount + state.rateLimitedCount;
}

function sourceRateTitle(state: TaskMonitorSourceRateState): string {
  if (state.sourceKey === "eastmoney_kline_cookie") {
    return "东方财富 K 线 Cookie";
  }
  return state.sourceKey || "unknown_source";
}

function sourceRateTone(state: TaskMonitorSourceRateState): "green" | "amber" | "red" {
  if (state.sourceKey === "eastmoney_kline_cookie") {
    if (state.state === "cooling") {
      return "red";
    }
    if (state.state === "healthy") {
      return "green";
    }
    return "amber";
  }
  if (state.rateLimitedCount > 0 || state.failureRate >= 0.2) {
    return "red";
  }
  if (state.timeoutCount > 0 || state.disconnectCount > 0 || state.failureRate >= 0.1) {
    return "amber";
  }
  return "green";
}

function sourceRateLabel(state: TaskMonitorSourceRateState): string {
  if (state.sourceKey === "eastmoney_kline_cookie") {
    if (state.state === "cooling") {
      return "Cookie 冷却中";
    }
    if (state.state === "healthy") {
      return "Cookie 可用";
    }
    return "等待 Cookie 探测";
  }
  const tone = sourceRateTone(state);
  if (tone === "red") {
    return "退避中";
  }
  if (tone === "amber") {
    return "降速观察";
  }
  return "稳定";
}

function formatRateInterval(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "-";
  }
  return seconds < 1 ? `${Math.round(seconds * 1000)}ms` : `${seconds.toFixed(seconds % 1 ? 1 : 0)}s`;
}

function TaskStatusPill({ task }: { task: TaskMonitorItem }) {
  return <span className={`status-pill task-status-pill tone-${task.tone}`}>{task.statusLabel}</span>;
}

function TaskProgressBar({
  ratio,
  tone,
  indeterminate = false,
}: {
  ratio: number;
  tone: string;
  indeterminate?: boolean;
}) {
  return (
    <div
      className={`task-progress-bar tone-${tone}${indeterminate ? " is-indeterminate" : ""}`}
      aria-label={indeterminate ? "进度不可量化，任务执行中" : `进度 ${formatPercent(ratio)}`}
    >
      <span style={indeterminate ? undefined : { width: formatPercent(ratio) }} />
    </div>
  );
}

function TaskMonitorMetric({
  icon,
  label,
  value,
  caption,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  caption: string;
  tone: "blue" | "green" | "amber" | "purple" | "red";
}) {
  return (
    <div className={`task-metric-card tone-${tone}`}>
      <span>{icon}</span>
      <div>
        <em>{label}</em>
        <strong>{value}</strong>
        <small>{caption}</small>
      </div>
    </div>
  );
}
