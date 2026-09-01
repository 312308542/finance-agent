import * as React from "react";
import { App, InputNumber, Segmented, Select, Switch, Tag } from "antd";
import { Database } from "lucide-react";
import {
  dataRecoveryApprove,
  dataRecoveryControl,
  dataRecoveryGetRun,
  dataRecoveryListRuns,
  dataRecoveryPreview,
  saveDataSyncConfig,
  startDataScheduler,
  stopDataScheduler,
} from "../api";
import type { ConsolePageProps } from "../consoleTypes";
import { DataTable, MetricBlock, Panel } from "../components/consoleCommon";
import { filterPreviewTasksByMarkets, filterSchedulerRuntimeTasks, marketsForPreset, pickEnabledMarkets, processingStatusLabel, schedulerRuntimeStatuses, schedulerRuntimeTaskDetail, schedulerStartFeedback, type SchedulerRuntimeStatus, type SchedulerStartFeedback, summarizeProcessingPlan, summarizeSchedulerQueue, summarizeSchedulerRuntime, summarizeSchedulerStatus, summarizeSchedulerWritePolicy } from "../dataSyncView";

type ResourcePoolConfig = {
  max_concurrent_jobs: number;
  description?: string;
  [key: string]: unknown;
};

const resourcePoolOrder = [
  "realtime",
  "collection_heavy",
  "article_enrichment",
  "analytics",
  "agent",
  "maintenance",
  "default",
];

const resourcePoolLabels: Record<string, { title: string; fallbackDescription: string }> = {
  realtime: {
    title: "盘中实时池",
    fallbackDescription: "盘中行情、风险情绪和触发器等轻量高优先级任务。",
  },
  collection_heavy: {
    title: "重采集池",
    fallbackDescription: "K 线、基本面、资金流、新闻列表等外部数据采集任务。",
  },
  article_enrichment: {
    title: "正文补抓池",
    fallbackDescription: "新闻正文二次补抓，避免慢任务占用主采集资源。",
  },
  analytics: {
    title: "分析计算池",
    fallbackDescription: "技术初筛、候选池合并、数据质量和推荐计算。",
  },
  agent: {
    title: "Agent 池",
    fallbackDescription: "Agent 事件消费和高风险复核，避免模型调用并发放大。",
  },
  maintenance: {
    title: "维护任务池",
    fallbackDescription: "回测、到期复盘等低频维护任务。",
  },
  default: {
    title: "默认兜底池",
    fallbackDescription: "未分类任务的兜底资源池。",
  },
};

const schedulerRuntimeStatusLabels: Record<SchedulerRuntimeStatus, string> = {
  scheduled: "计划中",
  blocked: "已阻塞",
  pending: "待执行",
  running: "运行中",
  completed: "已完成",
  failed: "已失败",
  cancelled: "已取消",
};

const schedulerRuntimeStatusColors: Record<SchedulerRuntimeStatus, string> = {
  scheduled: "blue",
  blocked: "orange",
  pending: "gold",
  running: "cyan",
  completed: "green",
  failed: "red",
  cancelled: "default",
};

function clampSchedulerConcurrency(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 4;
  }
  return Math.min(16, Math.max(1, Math.round(parsed)));
}

function normalizeResourcePools(
  value: unknown,
  fallbackMaxConcurrentJobs: number,
): Record<string, ResourcePoolConfig> {
  const raw = value && typeof value === "object" ? (value as Record<string, any>) : {};
  const normalized: Record<string, ResourcePoolConfig> = {};
  const names = Array.from(new Set([...resourcePoolOrder, ...Object.keys(raw)]));
  names.forEach((name) => {
    const payload = raw[name] && typeof raw[name] === "object" ? raw[name] : {};
    normalized[name] = {
      ...payload,
      max_concurrent_jobs: clampSchedulerConcurrency(
        payload.max_concurrent_jobs ?? (name === "default" ? fallbackMaxConcurrentJobs : 1),
      ),
      description: String(
        payload.description
          ?? resourcePoolLabels[name]?.fallbackDescription
          ?? "自定义调度资源池。",
      ),
    };
  });
  return normalized;
}

type RecoveryControlAction = "pause" | "resume" | "cancel";

const RECOVERY_ACTOR = "data-sync-console";

const recoveryRunStatusLabels: Record<string, { text: string; color: string }> = {
  draft: { text: "草稿", color: "default" },
  approved: { text: "已批准待执行", color: "blue" },
  running: { text: "执行中", color: "processing" },
  paused: { text: "已暂停", color: "warning" },
  verifying: { text: "校验中", color: "cyan" },
  attention_required: { text: "需人工关注", color: "error" },
  completed: { text: "已完成", color: "success" },
  completed_with_exceptions: { text: "完成但有例外", color: "warning" },
  cancelled: { text: "已取消", color: "default" },
};

const recoveryGateStatusLabels: Record<string, { text: string; color: string }> = {
  recovering: { text: "恢复中", color: "processing" },
  degraded: { text: "降级", color: "warning" },
  open: { text: "开放", color: "success" },
};

// 补跑阶段释义（规格第 7 节流水线编排）。
const recoveryPhaseLabels: Record<string, string> = {
  P0: "P0 冻结准备",
  P1: "P1 交易日历",
  P2: "P2 分区编排",
  P3: "P3 K线行情",
  P4: "P4 事实补齐",
  P5: "P5 残余复检",
  P6: "P6 质量验收",
  P7: "P7 派生刷新",
  P8: "P8 终验放行",
};

// 数据域中文释义（与调度任务数据域一一对应）。
const recoveryDomainLabels: Record<string, string> = {
  orchestration: "编排步骤（无独立采集）",
  market_calendar: "交易日历",
  market_bars: "A股K线行情",
  fundamentals: "基本面·财务指标",
  valuation: "估值快照",
  capital_flow: "资金流",
  events: "事件公告",
  risk_sentiment: "风险情绪",
};

function recoveryPhaseText(value: unknown): string {
  const key = recoveryText(value, "");
  return recoveryPhaseLabels[key] || key || "-";
}

function recoveryDomainText(value: unknown): string {
  const key = recoveryText(value, "");
  const label = recoveryDomainLabels[key];
  return label ? `${label}（${key}）` : key || "-";
}

const recoveryStepStatusLabels: Record<string, string> = {
  pending: "待执行",
  ready: "待执行",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

function recoveryBadge(
  labels: Record<string, { text: string; color: string }>,
  value: unknown,
): React.ReactNode {
  const key = typeof value === "string" ? value : "";
  if (!key) {
    return <Tag>未知</Tag>;
  }
  const label = labels[key];
  return <Tag color={label?.color}>{label?.text ?? key}</Tag>;
}

function recoveryStepStatusText(value: unknown): string {
  const key = typeof value === "string" ? value : "";
  return recoveryStepStatusLabels[key] || key || "-";
}

function recoveryText(value: unknown, fallback = "-"): string {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function recoverySnippet(value: unknown, max = 120): string {
  const text = recoveryText(value, "");
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function recoveryObjectList(value: unknown): Record<string, any>[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is Record<string, any> => Boolean(item) && typeof item === "object",
  );
}

function recoveryStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => recoveryText(item)).filter((item) => item !== "-");
}

function normalizeRecoveryRunsPayload(payload: Record<string, any>): Record<string, any>[] {
  if (Array.isArray(payload)) {
    return payload.filter((item) => item && typeof item === "object");
  }
  return recoveryObjectList(Array.isArray(payload?.runs) ? payload.runs : payload?.items);
}

function summarizeRecoveryExtras(detail: Record<string, any> | null | undefined): string[] {
  if (!detail) {
    return [];
  }
  const lines: string[] = [];
  const quality = detail.quality_result;
  if (quality && typeof quality === "object") {
    const bits: string[] = [];
    if (quality.status) {
      bits.push(`状态 ${recoveryText(quality.status)}`);
    }
    if (typeof quality.passed === "boolean") {
      bits.push(quality.passed ? "质检通过" : "质检未通过");
    }
    if (quality.checked_count !== undefined) {
      bits.push(`检查 ${recoveryText(quality.checked_count)} 项`);
    }
    if (quality.failed_count !== undefined) {
      bits.push(`失败 ${recoveryText(quality.failed_count)} 项`);
    }
    if (bits.length > 0) {
      lines.push(`质检结果：${bits.join("，")}`);
    }
  } else if (typeof quality === "string" && quality) {
    lines.push(`质检结果：${quality}`);
  }
  const progress = detail.live_progress;
  if (progress && typeof progress === "object") {
    const bits: string[] = [];
    if (progress.phase) {
      bits.push(`阶段 ${recoveryText(progress.phase)}`);
    }
    if (progress.completed_targets !== undefined || progress.total_targets !== undefined) {
      bits.push(
        `进度 ${recoveryText(progress.completed_targets ?? 0)} / ${recoveryText(progress.total_targets ?? 0)}`,
      );
    }
    if (progress.message) {
      bits.push(recoveryText(progress.message));
    }
    if (bits.length > 0) {
      lines.push(`实时进度：${bits.join("，")}`);
    }
  }
  return lines;
}


export function DataSyncControlPanel({
  dataSyncConfig,
  dataSchedulerStatus,
  refreshDataSync,
}: Pick<ConsolePageProps, "dataSyncConfig" | "dataSchedulerStatus" | "refreshDataSync">) {
  const config = dataSyncConfig?.data?.config ?? {};
  const validation = dataSyncConfig?.data?.validation ?? {};
  const preview = dataSyncConfig?.data?.preview ?? {};
  const schedulerPayload = dataSyncConfig?.data?.scheduler_payload ?? {};
  const tasks = Array.isArray(preview.tasks) ? preview.tasks : [];
  const processing = preview.processing ?? {};
  const processingStages = Array.isArray(processing.stages) ? processing.stages : [];
  const analytics = processing.analytics ?? {};
  const processingSummary = summarizeProcessingPlan(preview);
  const schedulerSummary = summarizeSchedulerStatus(dataSchedulerStatus);
  const enabledMarkets = pickEnabledMarkets(config);
  const persistedWritePolicy = summarizeSchedulerWritePolicy(dataSchedulerStatus);
  const configuredMaxConcurrentJobs = clampSchedulerConcurrency(
    config.max_concurrent_jobs ?? preview.max_concurrent_jobs ?? schedulerPayload.max_concurrent_jobs ?? 4,
  );
  const queueSummary = summarizeSchedulerQueue(dataSchedulerStatus, configuredMaxConcurrentJobs);
  const runtimeSummary = summarizeSchedulerRuntime(dataSchedulerStatus);
  const configuredResourcePools = normalizeResourcePools(
    config.resource_pools ?? schedulerPayload.resource_pools,
    configuredMaxConcurrentJobs,
  );
  const [preset, setPreset] = React.useState(config.preset ?? "personal-ashare");
  const [cacheBackend, setCacheBackend] = React.useState(config.cache_backend ?? "redis");
  const [enabled, setEnabled] = React.useState(config.enabled ?? true);
  const [markets, setMarkets] = React.useState<string[]>(enabledMarkets);
  const [maxConcurrentJobs, setMaxConcurrentJobs] = React.useState(
    clampSchedulerConcurrency(configuredMaxConcurrentJobs),
  );
  const [resourcePools, setResourcePools] = React.useState(configuredResourcePools);
  const [runtimeStatusFilter, setRuntimeStatusFilter] = React.useState<
    SchedulerRuntimeStatus | "all"
  >("all");
  const filteredRuntimeTasks = filterSchedulerRuntimeTasks(
    runtimeSummary.tasks,
    runtimeStatusFilter,
  );

  const [saving, setSaving] = React.useState(false);
  const [running, setRunning] = React.useState(false);
  const [saveStatus, setSaveStatus] = React.useState("未保存");
  const [schedulerStartFeedbackState, setSchedulerStartFeedbackState] =
    React.useState<SchedulerStartFeedback | null>(null);
  const currentSchedulerFeedback = schedulerStartFeedbackState ?? persistedWritePolicy;
  const { modal } = App.useApp();

  const [recoveryPreview, setRecoveryPreview] = React.useState<Record<string, any> | null>(null);
  const [recoveryPreviewing, setRecoveryPreviewing] = React.useState(false);
  const [recoveryRuns, setRecoveryRuns] = React.useState<Record<string, any>[]>([]);
  const [recoveryRunsLoaded, setRecoveryRunsLoaded] = React.useState(false);
  const [selectedRecoveryRunId, setSelectedRecoveryRunId] = React.useState("");
  const [recoveryRunDetail, setRecoveryRunDetail] = React.useState<Record<string, any> | null>(null);
  const [recoveryDetailLoading, setRecoveryDetailLoading] = React.useState(false);
  const [recoveryActing, setRecoveryActing] = React.useState(false);
  const [recoveryMessage, setRecoveryMessage] = React.useState("尚未检测停跑缺口");

  const visibleTasks = filterPreviewTasksByMarkets(tasks, markets);

  React.useEffect(() => {
    setPreset(config.preset ?? "personal-ashare");
    setCacheBackend(config.cache_backend ?? "redis");
    setEnabled(config.enabled ?? true);
    setMarkets(pickEnabledMarkets(config));
    setMaxConcurrentJobs(clampSchedulerConcurrency(configuredMaxConcurrentJobs));

    setResourcePools(configuredResourcePools);
  }, [
    config.preset,
    config.cache_backend,
    config.enabled,
    enabledMarkets.join(","),
    configuredMaxConcurrentJobs,

    JSON.stringify(configuredResourcePools),
  ]);

  React.useEffect(() => {
    setSchedulerStartFeedbackState(null);
  }, [
    persistedWritePolicy.modeLabel,
    persistedWritePolicy.statusText,
    persistedWritePolicy.writePolicy,
  ]);

  const toggleMarket = (market: string) => {
    setMarkets((current) =>
      current.includes(market) ? current.filter((item) => item !== market) : [...current, market],
    );
  };

  const changePreset = (nextPreset: string) => {

    setPreset(nextPreset);

    setMarkets(marketsForPreset(nextPreset));

  };

  const setResourcePoolLimit = (poolName: string, value: unknown) => {
    setResourcePools((current) => ({
      ...current,
      [poolName]: {
        ...(current[poolName] ?? {}),
        max_concurrent_jobs: clampSchedulerConcurrency(value),
      },
    }));
  };



  const saveConfig = async () => {
    setSaving(true);
    try {
      const result = await saveDataSyncConfig({
        preset,
        markets,
        enabled,
        cache_backend: cacheBackend,
        max_concurrent_jobs: maxConcurrentJobs,
        resource_pools: resourcePools,
      });
      setSaveStatus(result.status === "ok" ? "配置已保存" : result.message ?? "保存失败");
      await refreshDataSync?.();
    } finally {
      setSaving(false);
    }
  };

  const startScheduler = async (dryRun: boolean) => {
    setRunning(true);
    try {
      const result = await startDataScheduler({
        dry_run: dryRun,
        max_cycles: dryRun ? 1 : null,
      });
      const feedback = schedulerStartFeedback(dryRun, result);
      setSaveStatus(feedback.statusText);
      setSchedulerStartFeedbackState(feedback);
      await refreshDataSync?.();
    } finally {
      setRunning(false);
    }
  };

  const stopScheduler = async () => {
    setRunning(true);
    try {
      const result = await stopDataScheduler();
      setSaveStatus(result.status === "ok" ? "调度器已停止" : result.message ?? "停止失败");
      if (result.status === "ok") {
        setSchedulerStartFeedbackState({
          statusText: "调度器已停止",
          modeLabel: "已停止",
          writePolicy: "未写入",
        });
      }
      await refreshDataSync?.();
    } finally {
      setRunning(false);
    }
  };

  const refreshRecoveryRuns = async () => {
    const result = await dataRecoveryListRuns();
    if (result.status === "unavailable") {
      setRecoveryRuns([]);
      setRecoveryRunsLoaded(true);
      setRecoveryMessage(result.message ?? "停跑恢复接口不可用");
      return;
    }
    setRecoveryRuns(normalizeRecoveryRunsPayload(result));
    setRecoveryRunsLoaded(true);
  };

  const openRecoveryRunDetail = async (runId: string) => {
    if (!runId) {
      return;
    }
    setSelectedRecoveryRunId(runId);
    setRecoveryDetailLoading(true);
    try {
      const result = await dataRecoveryGetRun(runId);
      if (result.status === "unavailable") {
        setRecoveryRunDetail(null);
        setRecoveryMessage(result.message ?? ('批次 ' + runId + ' 详情获取失败'));
        return;
      }
      setRecoveryRunDetail(result);
      setRecoveryMessage('批次 ' + runId + ' 详情已刷新');
    } finally {
      setRecoveryDetailLoading(false);
    }
  };

  const detectRecoveryGap = async () => {
    setRecoveryPreviewing(true);
    try {
      const result = await dataRecoveryPreview();
      if (result.status === "unavailable") {
        setRecoveryMessage(result.message ?? "停跑恢复接口不可用");
        return;
      }
      setRecoveryPreview(result);
      const blockers = recoveryStringList(result.blockers);
      if (!result.executable) {
        setRecoveryMessage(
          blockers.length > 0
            ? '当前不满足自动补跑条件：' + blockers[0]
            : "当前不满足自动补跑条件，请查看阻断项",
        );
      } else {
        setRecoveryMessage(
          '缺口计划已生成：' + recoveryText(result.total_targets) + ' 个目标 / 约 ' + recoveryText(result.total_estimated_requests) + ' 个请求',
        );
      }
      const previewRunId = recoveryText(result.run_id, "");
      if (previewRunId) {
        setSelectedRecoveryRunId(previewRunId);
        await Promise.all([openRecoveryRunDetail(previewRunId), refreshRecoveryRuns()]);
      } else {
        await refreshRecoveryRuns();
      }
    } finally {
      setRecoveryPreviewing(false);
    }
  };

  const approveRecoveryRun = () => {
    const runId = selectedRecoveryRunId;
    const planHash = recoveryText(recoveryPreview?.plan_hash, "");
    if (!runId || !planHash) {
      setRecoveryMessage("缺少可执行的补跑计划，请先「检测缺口并生成计划」");
      return;
    }
    modal.confirm({
      title: "确认补跑",
      content: (
        <div>
          <p>批次 {runId} 将按当前页面所示计划真实补跑并写入数据库。</p>
          <p>
            plan_hash：<code>{planHash}</code>
          </p>
        </div>
      ),
      okText: "确认补跑",
      okButtonProps: { danger: true },
      cancelText: "再想想",
      onOk: async () => {
        setRecoveryActing(true);
        try {
          const result = await dataRecoveryApprove(runId, {
            plan_hash: planHash,
            approved_by: RECOVERY_ACTOR,
          });
          setRecoveryMessage(
            result.status === "unavailable"
              ? result.message ?? ('批次 ' + runId + ' 批准失败')
              : '批次 ' + runId + ' 已批准补跑',
          );
          await Promise.all([openRecoveryRunDetail(runId), refreshRecoveryRuns()]);
        } finally {
          setRecoveryActing(false);
        }
      },
    });
  };

  const controlRecoveryRun = (action: RecoveryControlAction) => {
    const runId = selectedRecoveryRunId;
    if (!runId) {
      return;
    }
    const actionLabels: Record<RecoveryControlAction, string> = {
      pause: "暂停补跑",
      resume: "恢复补跑",
      cancel: "取消补跑",
    };
    modal.confirm({
      title: actionLabels[action],
      content: <p>将对批次 {runId} 执行「{actionLabels[action]}」，操作会立即下发到调度器。</p>,
      okText: actionLabels[action],
      okButtonProps: action === "cancel" ? { danger: true } : undefined,
      cancelText: "取消",
      onOk: async () => {
        setRecoveryActing(true);
        try {
          const result = await dataRecoveryControl(runId, { action, actor: RECOVERY_ACTOR });
          setRecoveryMessage(
            result.status === "unavailable"
              ? result.message ?? ('批次 ' + runId + ' 操作失败')
              : '批次 ' + runId + ' 已执行：' + actionLabels[action],
          );
          await Promise.all([openRecoveryRunDetail(runId), refreshRecoveryRuns()]);
        } finally {
          setRecoveryActing(false);
        }
      },
    });
  };

  React.useEffect(() => {
    void refreshRecoveryRuns();
  }, []);

  const recoveryPreviewSteps = recoveryObjectList(recoveryPreview?.steps);
  const recoveryBlockers = recoveryStringList(recoveryPreview?.blockers);
  const recoveryWarnings = recoveryStringList(recoveryPreview?.warnings);
  const recoveryPlanHash = recoveryText(recoveryPreview?.plan_hash, "");
  const currentRecoveryStatus = recoveryText(
    recoveryRunDetail?.status ?? recoveryPreview?.run_status,
    "",
  );
  const currentGateStatus = recoveryText(
    recoveryRunDetail?.gate_status ?? recoveryPreview?.gate_status,
    "",
  );
  const canApproveRecovery =
    Boolean(recoveryPreview?.executable) && Boolean(recoveryPlanHash) && Boolean(selectedRecoveryRunId);
  const recoveryDetailSteps = recoveryObjectList(recoveryRunDetail?.steps);
  const recoveryDetailExceptions = recoveryObjectList(recoveryRunDetail?.exceptions);
  const recoveryExtraLines = summarizeRecoveryExtras(recoveryRunDetail);
  const showPauseButton = currentRecoveryStatus === "running";
  const showResumeButton = currentRecoveryStatus === "paused" || currentRecoveryStatus === "running";
  const showCancelButton = ["draft", "approved", "running", "paused"].includes(currentRecoveryStatus);

  return (
    <Panel title="数据同步控制台" subtitle="配置、预览、启动与停止本地基础数据调度器" icon={<Database size={16} />}>
      <div className="portfolio-strip">
        <MetricBlock label="配置预设" value={preset} />
        <MetricBlock label="已启用市场" value={markets.length} />
        <MetricBlock label="任务数" value={visibleTasks.length} />
        <MetricBlock label="调度状态" value={schedulerSummary.label} />
        <MetricBlock label="加工状态" value={processingSummary.analyticsLabel} />
        <MetricBlock label="写库策略" value={currentSchedulerFeedback.writePolicy} />
      </div>

      <div className="data-sync-status">
        <strong>{schedulerSummary.detail}</strong>
        <span>{saveStatus === "未保存" ? currentSchedulerFeedback.statusText : saveStatus}</span>
        <em>
          {currentSchedulerFeedback.modeLabel} · {validation.valid ? "配置已通过校验" : (validation.errors?.[0] ?? "配置未通过校验")}
        </em>
      </div>

      <div className="settings-grid">
        <section className="settings-block">
          <h3>配置项</h3>
          <label>
            <span>预设</span>
            <select value={preset} onChange={(event) => changePreset(event.target.value)}>
              <option value="personal-ashare">personal-ashare</option>

              <option value="personal-comprehensive">personal-comprehensive</option>
              <option value="ashare-comprehensive">ashare-comprehensive</option>
              <option value="crypto-comprehensive">crypto-comprehensive</option>
              <option value="lightweight">lightweight</option>
            </select>
          </label>
          <label>
            <span>缓存后端</span>
            <Select
              value={cacheBackend}
              onChange={(value) => setCacheBackend(value)}
              options={[
                { value: "redis", label: "redis" },
                { value: "auto", label: "auto" },
                { value: "null", label: "null" },
              ]}
            />
          </label>
          <label>
            <span>后台线程数</span>
            <InputNumber
              min={1}
              max={16}
              step={1}
              value={maxConcurrentJobs}
              onChange={(value) => setMaxConcurrentJobs(clampSchedulerConcurrency(value))}
              style={{ width: "100%" }}
            />
          </label>
          <div className="resource-pool-config">
            <div className="resource-pool-config-head">
              <strong>资源池配置</strong>
              <span>保存后调度器下一轮选任务前热生效，运行中的任务不会被中断。</span>
            </div>
            <div className="resource-pool-grid">
              {Object.entries(resourcePools).map(([poolName, pool]) => {
                const label = resourcePoolLabels[poolName];
                return (
                  <label key={poolName} className="resource-pool-card">
                    <span title={pool.description || label?.fallbackDescription}>
                      {label?.title ?? poolName}
                    </span>
                    <em>{poolName}</em>
                    <InputNumber
                      min={1}
                      max={16}
                      step={1}
                      size="small"
                      value={pool.max_concurrent_jobs}
                      onChange={(value) => setResourcePoolLimit(poolName, value)}
                      title={`${label?.title ?? poolName} 最大并发任务数`}
                    />
                  </label>
                );
              })}
            </div>
          </div>

          <div className="concurrency-strip">
            <span>运行 {queueSummary.runningJobs.length} / {queueSummary.maxConcurrentJobs}</span>
            <span>排队 {queueSummary.queuedJobs.length}</span>
          </div>
          <label className="toggle-row">
            <Switch checked={enabled} onChange={(checked) => setEnabled(checked)} />
            <span>启用调度器</span>
          </label>
          <div className="market-switches">
            {["ashare", "fund", "crypto_spot", "crypto_future"].map((market) => (
              <label key={market} className="toggle-row">
                <Switch
                  size="small"
                  checked={markets.includes(market)}
                  onChange={() => toggleMarket(market)}
                />
                <span>{market}</span>
              </label>
            ))}
          </div>
          <button className="button button-primary" onClick={() => void saveConfig()} disabled={saving}>
            保存配置
          </button>
        </section>

        <section className="settings-block">
          <h3>调度器操作</h3>
          <button
            className="button"
            onClick={() => void startScheduler(true)}
            disabled={running}
            title="只生成调度计划，不调用采集器，不写 PostgreSQL / TimescaleDB"
          >
            预演 1 轮（不入库）
          </button>
          <button
            className="button button-primary"
            onClick={() => void startScheduler(false)}
            disabled={running}
            title="启动真实采集，采集结果会写入 PostgreSQL / TimescaleDB"
          >
            启动真实同步（入库）
          </button>
          <button className="button" onClick={() => void stopScheduler()} disabled={running}>
            停止调度器
          </button>
          <div className="route-preview">
            <strong>当前状态</strong>
            <p>{schedulerSummary.label}</p>
            <p>
              {runtimeSummary.source === "postgresql"
                ? "PostgreSQL 调度快照可用"
                : dataSchedulerStatus?.health?.source === "status_file"
                  ? "PostgreSQL 不可用，正在读取状态文件"
                  : "正在连接调度运行快照"}
            </p>
            <p>线程 {queueSummary.runningJobs.length} / {queueSummary.maxConcurrentJobs}，排队 {queueSummary.queuedJobs.length}</p>
            <p>{currentSchedulerFeedback.modeLabel} / {currentSchedulerFeedback.writePolicy}</p>
            <p>{dataSyncConfig?.data?.config_file ?? "未配置文件"}</p>
          </div>
        </section>

        <section className="settings-block settings-block-wide">
          <div className="scheduler-runtime-head">
            <div>
              <h3>调度运行快照</h3>
              <span>来源 {runtimeSummary.source === "postgresql" ? "PostgreSQL" : "不可用"}</span>
            </div>
            <Tag color={runtimeSummary.configDrift ? "red" : runtimeSummary.configDriftStatus === "match" ? "green" : "default"}>
              {runtimeSummary.configDrift
                ? "配置摘要不一致"
                : runtimeSummary.configDriftStatus === "match"
                  ? "配置摘要一致"
                  : "配置摘要待确认"}
            </Tag>
          </div>

          {runtimeSummary.configDrift && (
            <div className="data-sync-status runtime-config-drift" role="alert">
              <strong>配置摘要不一致</strong>
              <span>scheduler {runtimeSummary.schedulerConfigDigest ?? "unknown"}</span>
              <em>API {runtimeSummary.apiConfigDigest ?? "unknown"}</em>
            </div>
          )}

          <div className="portfolio-strip scheduler-runtime-counts">
            {schedulerRuntimeStatuses.map((status) => (
              <MetricBlock
                key={status}
                label={`${schedulerRuntimeStatusLabels[status]}（累计）`}
                value={runtimeSummary.statusCounts[status]}
              />
            ))}
          </div>

          <Segmented
            block
            className="scheduler-runtime-filter"
            value={runtimeStatusFilter}
            onChange={(value) => setRuntimeStatusFilter(value as SchedulerRuntimeStatus | "all")}
            options={[
              { value: "all", label: `全部 ${runtimeSummary.tasks.length}` },
              ...schedulerRuntimeStatuses.map((status) => ({
                value: status,
                label: `${schedulerRuntimeStatusLabels[status]} ${runtimeSummary.listedStatusCounts[status]}`,
              })),
            ]}
          />

          <DataTable
            columns={["任务", "状态", "资源池", "优先级", "计划时间", "运行详情"]}
            rows={filteredRuntimeTasks.map((task) => [
              task.job_name,
              <Tag key={`${task.task_id}:status`} color={schedulerRuntimeStatusColors[task.status]}>
                {schedulerRuntimeStatusLabels[task.status]}
              </Tag>,
              task.resource_pool ?? "default",
              task.priority ?? 100,
              task.scheduled_for ?? "-",
              schedulerRuntimeTaskDetail(task),
            ])}
            emptyText={
              runtimeSummary.source === "postgresql"
                ? "当前筛选没有持久任务"
                : "正在等待 PostgreSQL 调度快照"
            }
          />
        </section>

        <section className="settings-block settings-block-wide">
          <h3>数据加工链路</h3>
          <div className="processing-summary">
            <MetricBlock label="清洗归一化" value={processingSummary.normalizationLabel} />
            <MetricBlock label="指标因子链路" value={processingSummary.analyticsLabel} />
            <MetricBlock label="加工阶段" value={processingSummary.stageCount} />
            <MetricBlock label="运行输入" value={processingSummary.requiredInput} />
          </div>
          <div className="data-sync-status">
            <strong>{analytics.default_pipeline ?? "UniverseRecommendationPipeline.run_for_universe"}</strong>
            <span>{analytics.notes?.[0] ?? "指标、因子、评分、信号和推荐流水线已有服务，但还没有注册为基础数据常驻 job。"}</span>
            <em>
              候选池示例：
              {(analytics.candidate_universe_patterns ?? []).slice(0, 3).join("，") || "暂无候选池"}
            </em>
          </div>
          <DataTable
            columns={["阶段", "状态", "触发", "输入", "输出"]}
            rows={processingStages.map((stage: any) => [
              stage.title,
              processingStatusLabel(stage.scheduler_status ?? stage.status),
              stage.trigger,
              (stage.inputs ?? []).slice(0, 3).join(" / "),
              (stage.outputs ?? []).slice(0, 3).join(" / "),
            ])}
            emptyText="暂无数据加工链路预览"
          />
        </section>

        <section className="settings-block settings-block-wide">
          <h3>任务预览</h3>
          <DataTable
            columns={["任务", "市场", "模式", "间隔", "说明"]}
            rows={visibleTasks.slice(0, 12).map((task: any) => [
              task.title,
              task.market,
              task.mode,
              `${task.interval_seconds}s`,
              task.notes?.[0] ?? task.task_key,
            ])}
            emptyText="暂无调度任务预览"
          />
        </section>

        <section className="settings-block settings-block-wide">
          <h3>停跑恢复</h3>
          <div className="portfolio-strip">
            <MetricBlock
              label="批次状态"
              value={currentRecoveryStatus ? recoveryBadge(recoveryRunStatusLabels, currentRecoveryStatus) : "无批次"}
            />
            <MetricBlock
              label="门控状态"
              value={currentGateStatus ? recoveryBadge(recoveryGateStatusLabels, currentGateStatus) : "未知"}
            />
            <MetricBlock label="目标数" value={recoveryPreview ? recoveryText(recoveryPreview.total_targets) : "-"} />
            <MetricBlock
              label="预计请求量"
              value={recoveryPreview ? recoveryText(recoveryPreview.total_estimated_requests) : "-"}
            />
            <MetricBlock
              label="补跑截止日"
              value={recoveryText(recoveryRunDetail?.cutoff_date ?? recoveryPreview?.cutoff_date)}
            />
          </div>

          <div className="data-sync-status">
            <strong>{selectedRecoveryRunId ? ('当前批次 ' + selectedRecoveryRunId) : "尚未选择补跑批次"}</strong>
            <span>{recoveryMessage}</span>
            <em>
              {recoveryPlanHash ? ('plan_hash ' + recoveryPlanHash) : "plan_hash 待生成"}
              {" · "}
              操作人 {RECOVERY_ACTOR}
            </em>
          </div>

          <button className="button" onClick={() => void detectRecoveryGap()} disabled={recoveryPreviewing}>
            {recoveryPreviewing ? "检测中…" : "检测缺口并生成计划"}
          </button>
          <button
            className="button button-primary"
            onClick={approveRecoveryRun}
            disabled={!canApproveRecovery || recoveryActing}
            title={
              recoveryPlanHash
                ? '按页面所示 plan_hash ' + recoveryPlanHash + ' 批准补跑'
                : "请先检测缺口生成计划"
            }
          >
            确认补跑
          </button>
          {showPauseButton && (
            <button className="button" onClick={() => controlRecoveryRun("pause")} disabled={recoveryActing}>
              暂停补跑
            </button>
          )}
          {showResumeButton && (
            <button className="button" onClick={() => controlRecoveryRun("resume")} disabled={recoveryActing}>
              恢复补跑
            </button>
          )}
          {showCancelButton && (
            <button className="button" onClick={() => controlRecoveryRun("cancel")} disabled={recoveryActing}>
              取消补跑
            </button>
          )}

          {recoveryBlockers.length > 0 && (
            <div className="data-sync-status">
              <strong>阻断项</strong>
              <span>{recoveryBlockers.join("；")}</span>
            </div>
          )}
          {recoveryWarnings.length > 0 && (
            <div className="data-sync-status">
              <strong>警告</strong>
              <span>{recoveryWarnings.join("；")}</span>
            </div>
          )}

          <DataTable
            columns={["阶段", "数据域", "目标数", "预计请求量"]}
            rows={recoveryPreviewSteps.map((step: any) => [
              recoveryPhaseText(step.phase),
              recoveryDomainText(step.data_domain),
              recoveryText(step.target_count),
              recoveryText(step.estimated_requests),
            ])}
            emptyText={recoveryPreview ? "补跑计划未包含步骤" : "尚未生成补跑计划"}
          />

          <DataTable
            columns={["批次", "市场", "状态", "门控", "截止日", "创建时间", "操作"]}
            rows={recoveryRuns.map((run: any) => [
              recoveryText(run.run_id),
              recoveryText(run.market),
              recoveryBadge(recoveryRunStatusLabels, run.status),
              recoveryBadge(recoveryGateStatusLabels, run.gate_status),
              recoveryText(run.cutoff_date),
              recoveryText(run.created_at),
              <button
                className="button"
                onClick={() => void openRecoveryRunDetail(recoveryText(run.run_id, ""))}
                disabled={recoveryDetailLoading}
              >
                查看详情
              </button>,
            ])}
            emptyText={recoveryRunsLoaded ? "暂无停跑补跑批次" : "正在加载批次列表…"}
          />

          {recoveryRunDetail ? (
            <>
              <div className="route-preview">
                <strong>批次详情 {recoveryText(recoveryRunDetail.run_id)}</strong>
                <p>
                  状态 {recoveryBadge(recoveryRunStatusLabels, recoveryRunDetail.status)}
                  {" · "}
                  门控 {recoveryBadge(recoveryGateStatusLabels, recoveryRunDetail.gate_status)}
                  {" · "}
                  截止 {recoveryText(recoveryRunDetail.cutoff_date)}
                </p>
                {recoveryExtraLines.map((line) => (
                  <p key={line}>{line}</p>
                ))}
              </div>
              <DataTable
                columns={["阶段", "数据域", "状态", "目标数", "已完成", "可重试", "例外"]}
                rows={recoveryDetailSteps.map((step: any) => [
                  recoveryPhaseText(step.phase),
                  recoveryDomainText(step.data_domain),
                  recoveryStepStatusText(step.status),
                  recoveryText(step.target_count),
                  recoveryText(step.completed_count),
                  recoveryText(step.retryable_count),
                  recoveryText(step.exception_count),
                ])}
                emptyText="该批次暂无步骤明细"
              />
              <DataTable
                columns={["数据域", "资产", "分类", "证据摘要"]}
                rows={recoveryDetailExceptions.map((item: any) => [
                  recoveryText(item.data_domain),
                  recoveryText(item.asset_id ?? item.target_id),
                  recoveryText(item.exception_code),
                  recoverySnippet(item.evidence, 90) +
                    (item.last_error ? '；最近错误：' + recoverySnippet(item.last_error, 60) : ""),
                ])}
                emptyText="该批次暂无例外记录"
              />
            </>
          ) : (
            <div className="route-preview">
              <strong>批次详情</strong>
              <p>
                {recoveryDetailLoading ? "正在加载批次详情…" : "从上方批次列表选择一个批次，查看步骤进度与例外清单。"}
              </p>
            </div>
          )}
        </section>
      </div>
    </Panel>
  );
}
