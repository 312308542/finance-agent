import * as React from "react";
import { Database } from "lucide-react";
import { saveDataSyncConfig, startDataScheduler, stopDataScheduler } from "../api";
import type { ConsolePageProps } from "../consoleTypes";
import { DataTable, MetricBlock, Panel, TaskQueueList } from "../components/consoleCommon";
import { pickEnabledMarkets, processingStatusLabel, schedulerStartFeedback, type SchedulerStartFeedback, summarizeProcessingPlan, summarizeSchedulerQueue, summarizeSchedulerStatus, summarizeSchedulerWritePolicy } from "../dataSyncView";

function clampSchedulerConcurrency(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 4;
  }
  return Math.min(16, Math.max(1, Math.round(parsed)));
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
  const [preset, setPreset] = React.useState(config.preset ?? "personal-comprehensive");
  const [cacheBackend, setCacheBackend] = React.useState(config.cache_backend ?? "redis");
  const [enabled, setEnabled] = React.useState(config.enabled ?? true);
  const [markets, setMarkets] = React.useState<string[]>(enabledMarkets);
  const [maxConcurrentJobs, setMaxConcurrentJobs] = React.useState(
    clampSchedulerConcurrency(configuredMaxConcurrentJobs),
  );
  const [saving, setSaving] = React.useState(false);
  const [running, setRunning] = React.useState(false);
  const [saveStatus, setSaveStatus] = React.useState("未保存");
  const [schedulerStartFeedbackState, setSchedulerStartFeedbackState] =
    React.useState<SchedulerStartFeedback | null>(null);
  const currentSchedulerFeedback = schedulerStartFeedbackState ?? persistedWritePolicy;

  React.useEffect(() => {
    setPreset(config.preset ?? "personal-comprehensive");
    setCacheBackend(config.cache_backend ?? "redis");
    setEnabled(config.enabled ?? true);
    setMarkets(pickEnabledMarkets(config));
    setMaxConcurrentJobs(clampSchedulerConcurrency(configuredMaxConcurrentJobs));
  }, [
    config.preset,
    config.cache_backend,
    config.enabled,
    enabledMarkets.join(","),
    configuredMaxConcurrentJobs,
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

  const saveConfig = async () => {
    setSaving(true);
    try {
      const result = await saveDataSyncConfig({
        preset,
        markets,
        enabled,
        cache_backend: cacheBackend,
        max_concurrent_jobs: maxConcurrentJobs,
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

  return (
    <Panel title="数据同步控制台" subtitle="配置、预览、启动与停止本地基础数据调度器" icon={<Database size={16} />}>
      <div className="portfolio-strip">
        <MetricBlock label="配置预设" value={config.preset ?? "personal-comprehensive"} />
        <MetricBlock label="已启用市场" value={enabledMarkets.length} />
        <MetricBlock label="任务数" value={validation.task_count ?? tasks.length ?? 0} />
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
            <select value={preset} onChange={(event) => setPreset(event.target.value)}>
              <option value="personal-comprehensive">personal-comprehensive</option>
              <option value="ashare-comprehensive">ashare-comprehensive</option>
              <option value="crypto-comprehensive">crypto-comprehensive</option>
              <option value="lightweight">lightweight</option>
            </select>
          </label>
          <label>
            <span>缓存后端</span>
            <select value={cacheBackend} onChange={(event) => setCacheBackend(event.target.value)}>
              <option value="redis">redis</option>
              <option value="auto">auto</option>
              <option value="null">null</option>
            </select>
          </label>
          <label>
            <span>后台线程数</span>
            <input
              type="number"
              min={1}
              max={16}
              step={1}
              value={maxConcurrentJobs}
              onChange={(event) =>
                setMaxConcurrentJobs(clampSchedulerConcurrency(event.target.value))
              }
            />
          </label>
          <div className="concurrency-strip">
            <span>运行 {queueSummary.runningJobs.length} / {queueSummary.maxConcurrentJobs}</span>
            <span>排队 {queueSummary.queuedJobs.length}</span>
          </div>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            <span>启用调度器</span>
          </label>
          <div className="market-switches">
            {["ashare", "fund", "crypto_spot", "crypto_future"].map((market) => (
              <label key={market} className="toggle-row">
                <input
                  type="checkbox"
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
            <p>{dataSchedulerStatus?.health?.status ?? "missing"}</p>
            <p>{dataSchedulerStatus?.process?.running ? "进程运行中" : "进程未运行"}</p>
            <p>线程 {queueSummary.runningJobs.length} / {queueSummary.maxConcurrentJobs}，排队 {queueSummary.queuedJobs.length}</p>
            <p>{currentSchedulerFeedback.modeLabel} / {currentSchedulerFeedback.writePolicy}</p>
            <p>{dataSyncConfig?.data?.config_file ?? "未配置文件"}</p>
          </div>
        </section>

        <section className="settings-block settings-block-wide">
          <h3>任务队列</h3>
          <div className="scheduler-queue-grid">
            <TaskQueueList
              title={`运行中 ${queueSummary.runningJobs.length}`}
              jobs={queueSummary.runningJobs}
              emptyText="暂无运行任务"
            />
            <TaskQueueList
              title={`等待中 ${queueSummary.queuedJobs.length}`}
              jobs={queueSummary.queuedJobs}
              emptyText="暂无排队任务"
            />
          </div>
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
            rows={tasks.slice(0, 12).map((task: any) => [
              task.title,
              task.market,
              task.mode,
              `${task.interval_seconds}s`,
              task.notes?.[0] ?? task.task_key,
            ])}
            emptyText="暂无调度任务预览"
          />
        </section>
      </div>
    </Panel>
  );
}
