import React from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  Clock3,
  Database,
  FileText,
  GitBranch,
  LineChart,
  ListChecks,
  Eye,
  EyeOff,
  MessageSquareText,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCcw,
  Search,
  ServerCog,
  Settings2,
  ShieldAlert,
  Trash2,
  Wallet,
  Wifi,
  X,
} from "lucide-react";
import {
  buildModelProviderConnectivityPayload,
  cancelDataSchedulerJob,
  DashboardSummary,
  deleteModelInstance,
  loadDataSchedulerJobs,
  loadDataSchedulerProgress,
  loadDataSchedulerStatus,
  loadDataSyncConfig,
  loadDashboardSummary,
  loadModelRoutePreview,
  pauseDataSchedulerJob,
  revealModelProviderSecret,
  rerunFailedDataSchedulerJob,
  resumeDataSchedulerJob,
  runDataSchedulerJob,
  saveDataSyncConfig,
  saveModelInstance,
  saveModelProvider,
  saveModelRoute,
  startDataScheduler,
  statusText,
  stopDataScheduler,
  streamChatMessage,
  testModelProviderConnectivity,
  toneByStatus,
  updateDataSchedulerJob,
} from "./api";
import { buildAgentModelNodes } from "./agentModelRoutes";
import {
  pickEnabledMarkets,
  processingStatusLabel,
  schedulerStartFeedback,
  type SchedulerStartFeedback,
  summarizeProcessingPlan,
  summarizeSchedulerQueue,
  summarizeSchedulerStatus,
  summarizeSchedulerWritePolicy,
} from "./dataSyncView";
import {
  OPENAI_COMPATIBLE_PROVIDER_KEY,
  OPENAI_COMPATIBLE_PROVIDER_NAME,
  modelRoleLabel,
  openAICompatibleProviderKeyForModel,
  routeStatusLabel,
  summarizeOpenAICompatibleConfig,
} from "./modelConfigView";
import {
  buildTaskMonitorModel,
  estimateRemainingSeconds,
  extractFallbackWaitingTasks,
  extractSchedulerJobTasks,
  filterTaskMonitorItems,
  formatCompactNumber,
  formatDateTime,
  formatDuration,
  formatPercent,
  formatTaskLogLine,
  statusTone,
  taskMonitorFilters,
  type TaskMonitorFilter,
  type TaskMonitorItem,
  type TaskMonitorSourceRateState,
} from "./taskMonitorView";
import { ReportPage } from "./pages/ReportPage";
import { RecommendationPage } from "./pages/RecommendationPage";
import "./styles.css";

const ownerId = "default-owner";

type NavId =
  | "overview"
  | "portfolio"
  | "watchlist"
  | "recommendation"
  | "risk"
  | "agent"
  | "report"
  | "memory"
  | "data"
  | "tasks"
  | "model";

const navigation: Array<{ id: NavId; label: string; icon: React.ElementType }> = [
  { id: "overview", label: "总览", icon: Activity },
  { id: "portfolio", label: "持仓监控", icon: Wallet },
  { id: "watchlist", label: "观察池", icon: ListChecks },
  { id: "recommendation", label: "推荐决策", icon: BarChart3 },
  { id: "risk", label: "风险中心", icon: ShieldAlert },
  { id: "agent", label: "Agent 运行", icon: Brain },
  { id: "report", label: "中文报告", icon: FileText },
  { id: "memory", label: "Finance Memory", icon: GitBranch },
  { id: "data", label: "数据同步", icon: Database },
  { id: "tasks", label: "任务监控", icon: ServerCog },
  { id: "model", label: "模型配置", icon: Settings2 },
];

const pageMeta: Record<NavId, { eyebrow: string; title: string }> = {
  overview: { eyebrow: "Operations Console", title: "总览工作台" },
  portfolio: { eyebrow: "Portfolio Monitor", title: "持仓监控" },
  watchlist: { eyebrow: "Watchlist Console", title: "观察池管理" },
  recommendation: { eyebrow: "Recommendation Desk", title: "推荐决策" },
  risk: { eyebrow: "Risk Center", title: "风险中心" },
  agent: { eyebrow: "Agent Runtime", title: "Agent 运行" },
  report: { eyebrow: "Chinese Report", title: "中文报告" },
  memory: { eyebrow: "Finance Memory", title: "金融记忆" },
  data: { eyebrow: "Data Sync", title: "数据同步" },
  tasks: { eyebrow: "Task Monitor", title: "任务监控" },
  model: { eyebrow: "Model Routing", title: "模型配置" },
};

type ChatLine = {
  id: string;
  role: "assistant" | "user" | "status" | "error";
  content: string;
  label?: string;
};

const CHAT_TYPEWRITER_INTERVAL_MS = 18;

function takeTypewriterChunk(buffer: string): string {
  const chars = Array.from(buffer);
  if (!chars.length) {
    return "";
  }
  const first = chars[0];
  if (first === "\n" || /[\s，。；：、,.!?！？;:]/.test(first)) {
    return first;
  }
  const chunkSize = chars.length > 120 ? 4 : chars.length > 48 ? 2 : 1;
  return chars.slice(0, chunkSize).join("");
}

function appendChatStatusLine(
  lines: ChatLine[],
  content: string,
  id = `status-${Date.now()}`,
  label = "流程",
): ChatLine[] {
  if (!content) {
    return lines;
  }
  const lastLine = lines[lines.length - 1];
  if (lastLine?.role === "status" && lastLine.content === content) {
    return lines;
  }
  return [...lines, { id, role: "status", content, label }];
}

function insertChatStatusLineBefore(
  lines: ChatLine[],
  beforeId: string,
  content: string,
  label = "流程",
): ChatLine[] {
  if (!content || lines.some((line) => line.role === "status" && line.content === content)) {
    return lines;
  }
  const beforeIndex = lines.findIndex((line) => line.id === beforeId);
  if (beforeIndex < 0) {
    return appendChatStatusLine(lines, content, undefined, label);
  }
  const nextLines = [...lines];
  nextLines.splice(beforeIndex, 0, {
    id: `status-${beforeId}-${Date.now()}-${lines.length}`,
    role: "status",
    content,
    label,
  });
  return nextLines;
}

function App() {
  const [summary, setSummary] = React.useState<DashboardSummary | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [activeNav, setActiveNav] = React.useState<NavId>("overview");
  const [reportWorkflowRunId, setReportWorkflowRunId] = React.useState<string | null>(null);
  const [message, setMessage] = React.useState("");
  const [chatLines, setChatLines] = React.useState<ChatLine[]>([
    {
      id: "welcome",
      role: "assistant",
      content: "聊天窗口已就绪。可以询问持仓、观察池、风险、模型配置或 Workflow。",
    },
  ]);
  const [chatSessionId, setChatSessionId] = React.useState<string | null>(null);
  const [chatOpen, setChatOpen] = React.useState(false);
  const [chatStreaming, setChatStreaming] = React.useState(false);
  const [chatStatus, setChatStatus] = React.useState("就绪");
  const [modelPreview, setModelPreview] = React.useState<Record<string, any> | null>(null);
  const [dataSyncConfig, setDataSyncConfig] = React.useState<Record<string, any> | null>(null);
  const [dataSchedulerStatus, setDataSchedulerStatus] = React.useState<Record<string, any> | null>(null);
  const [taskSchedulerProgress, setTaskSchedulerProgress] = React.useState<Record<string, any> | null>(null);
  const [taskSchedulerJobs, setTaskSchedulerJobs] = React.useState<Record<string, any> | null>(null);
  const [taskMonitorLoading, setTaskMonitorLoading] = React.useState(true);

  const refresh = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await loadDashboardSummary(ownerId);
      setSummary(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  React.useEffect(() => {
    const applyHashRoute = () => {
      const hash = window.location.hash || "";
      if (!hash.startsWith("#report:")) {
        return;
      }
      const runId = decodeURIComponent(hash.slice("#report:".length));
      setReportWorkflowRunId(runId || null);
      setActiveNav("report");
    };
    applyHashRoute();
    window.addEventListener("hashchange", applyHashRoute);
    return () => window.removeEventListener("hashchange", applyHashRoute);
  }, []);

  const refreshDataSync = React.useCallback(async () => {
    const [config, status] = await Promise.all([
      loadDataSyncConfig(),
      loadDataSchedulerStatus(),
    ]);
    setDataSyncConfig(config);
    setDataSchedulerStatus(status);
  }, []);

  React.useEffect(() => {
    void refreshDataSync();
  }, [refreshDataSync, summary?.generated_at]);

  const refreshTaskMonitor = React.useCallback(async (silent = false) => {
    if (!silent) {
      setTaskMonitorLoading(true);
    }
    try {
      const [progress, jobs] = await Promise.all([
        loadDataSchedulerProgress(120),
        loadDataSchedulerJobs(),
      ]);
      setTaskSchedulerProgress(progress);
      setTaskSchedulerJobs(jobs);
    } finally {
      setTaskMonitorLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void refreshTaskMonitor();
    const timer = window.setInterval(() => {
      void refreshTaskMonitor(true);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [refreshTaskMonitor]);

  const refreshModelPreview = React.useCallback(async () => {
    const preview = await loadModelRoutePreview();
    setModelPreview(preview);
  }, []);

  React.useEffect(() => {
    void refreshModelPreview();
  }, [refreshModelPreview, summary?.generated_at]);

  const sendMessage = async () => {
    const content = message.trim();
    if (!content || chatStreaming) {
      return;
    }
    const assistantId = `assistant-${Date.now()}`;
    let pendingAssistantText = "";
    let typingTimer: number | null = null;
    let streamEnded = false;
    let resolveTypingDone: (() => void) | null = null;
    const typingDone = new Promise<void>((resolve) => {
      resolveTypingDone = resolve;
    });
    const resolveTypingIfIdle = () => {
      if (streamEnded && !pendingAssistantText && typingTimer === null) {
        resolveTypingDone?.();
      }
    };
    const appendAssistantText = (chunk: string) => {
      setChatLines((lines) =>
        lines.map((line) =>
          line.id === assistantId ? { ...line, content: `${line.content}${chunk}` } : line,
        ),
      );
    };
    const scheduleTyping = () => {
      if (typingTimer !== null) {
        return;
      }
      typingTimer = window.setTimeout(() => {
        typingTimer = null;
        const chunk = takeTypewriterChunk(pendingAssistantText);
        pendingAssistantText = pendingAssistantText.slice(chunk.length);
        if (chunk) {
          appendAssistantText(chunk);
        }
        if (pendingAssistantText) {
          scheduleTyping();
        } else {
          resolveTypingIfIdle();
        }
      }, CHAT_TYPEWRITER_INTERVAL_MS);
    };
    const enqueueAssistantText = (delta: string) => {
      if (!delta) {
        return;
      }
      pendingAssistantText += delta;
      scheduleTyping();
    };
    const finishTyping = async () => {
      streamEnded = true;
      resolveTypingIfIdle();
      scheduleTyping();
      await typingDone;
    };
    const cancelTyping = () => {
      if (typingTimer !== null) {
        window.clearTimeout(typingTimer);
        typingTimer = null;
      }
      pendingAssistantText = "";
      streamEnded = true;
      resolveTypingDone?.();
    };
    setMessage("");
    setChatOpen(true);
    setChatStreaming(true);
    setChatStatus("连接中");
    setChatLines((lines) => [
      ...lines,
      { id: `user-${Date.now()}`, role: "user", content },
      { id: assistantId, role: "assistant", content: "" },
    ]);
    try {
      await streamChatMessage(ownerId, content, {
        sessionId: chatSessionId,
        onEvent: (event) => {
          if (event.event === "status") {
            setChatStatus(String(event.data?.message ?? "处理中"));
            if (event.data?.chat_session_id) {
              setChatSessionId(String(event.data.chat_session_id));
            }
            return;
          }
          if (
            event.event === "agent_step" ||
            event.event === "workflow_step" ||
            event.event === "model_call" ||
            event.event === "model_result"
          ) {
            const eventMessage = String(event.data?.message ?? event.event);
            const label =
              event.event === "agent_step"
                ? "Agent"
                : event.event === "workflow_step"
                  ? "流程"
                  : "模型";
            setChatStatus(eventMessage);
            setChatLines((lines) =>
              insertChatStatusLineBefore(lines, assistantId, eventMessage, label),
            );
            return;
          }
          if (event.event === "tool_call" || event.event === "tool_result") {
            const tool = String(event.data?.tool ?? "工具");
            const eventMessage = String(event.data?.message ?? "");
            const statusLine =
              event.event === "tool_call"
                ? `调用工具：${tool}`
                : `工具完成：${eventMessage || tool}`;
            setChatStatus(event.event === "tool_call" ? "调用工具" : "工具完成");
            setChatLines((lines) =>
              insertChatStatusLineBefore(lines, assistantId, statusLine, "工具"),
            );
            return;
          }
          if (event.event === "delta") {
            const delta = String(event.data?.content ?? "");
            enqueueAssistantText(delta);
            return;
          }
          if (event.event === "done") {
            if (event.data?.chat_session_id) {
              setChatSessionId(String(event.data.chat_session_id));
            }
            setChatStatus("完成");
            return;
          }
          if (event.event === "error") {
            cancelTyping();
            setChatStatus("失败");
            setChatLines((lines) =>
              lines.map((line) =>
                line.id === assistantId
                  ? { ...line, role: "error", content: String(event.data?.message ?? "聊天服务暂不可用") }
                  : line,
              ),
            );
          }
        },
      });
      await finishTyping();
    } catch (err) {
      cancelTyping();
      setChatStatus("失败");
      setChatLines((lines) =>
        lines.map((line) =>
          line.id === assistantId
            ? {
                ...line,
                role: "error",
                content: err instanceof Error ? err.message : String(err),
              }
            : line,
        ),
      );
    }
    setChatStreaming(false);
  };

  const sections = summary?.sections;
  const portfolio = sections?.portfolio;
  const watchlists = sections?.watchlists;
  const recommendations = sections?.recommendations;
  const risks = sections?.risks;
  const workflows = sections?.workflows;
  const memories = sections?.memories;
  const dataHealth = sections?.data_health;
  const models = sections?.models;
  const currentPage = pageMeta[activeNav];
  const isTaskMonitorPage = activeNav === "tasks";

  return (
    <main className={isTaskMonitorPage ? "terminal-app terminal-app-task-monitor" : "terminal-app"}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <LineChart size={20} />
          </div>
          <div>
            <strong>Finance Agent</strong>
            <span>私人金融助手控制台</span>
          </div>
        </div>

        <nav className="nav-list" aria-label="主导航">
          {navigation.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={activeNav === item.id ? "nav-item is-active" : "nav-item"}
                aria-current={activeNav === item.id ? "page" : undefined}
                onClick={() => setActiveNav(item.id)}
              >
                <Icon size={17} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <span>运行模式</span>
          <strong>{summary?.source === "fallback" ? "演示降级" : "真实接口"}</strong>
        </div>
      </aside>

      <section className={isTaskMonitorPage ? "workspace workspace-task-monitor" : "workspace"}>
        {!isTaskMonitorPage ? (
          <header className="topbar">
            <div>
              <p className="eyebrow">{currentPage.eyebrow}</p>
              <h1>{currentPage.title}</h1>
            </div>
            <div className="topbar-actions">
              <label className="search-box">
                <Search size={15} />
                <input placeholder="搜索标的、报告、记忆" />
              </label>
              <button
                className="button"
                onClick={() => {
                  void refresh();
                  void refreshDataSync();
                  void refreshTaskMonitor();
                }}
                disabled={loading}
              >
                <RefreshCcw size={15} />
                {loading ? "刷新中" : "刷新"}
              </button>
            </div>
          </header>
        ) : null}

        {error ? <div className="notice notice-red">{error}</div> : null}

        {!isTaskMonitorPage ? (
          <section className="status-grid">
            <StatusCard
              icon={<Database size={17} />}
              label="事实数据库"
              value="PostgreSQL + TimescaleDB"
              status={summary?.status ?? "partial"}
            />
            <StatusCard
              icon={<GitBranch size={17} />}
              label="知识图谱"
              value="Neo4j / DozerDB"
              status={memories?.status ?? "empty"}
            />
            <StatusCard
              icon={<ServerCog size={17} />}
              label="数据调度"
              value={`${dataHealth?.metrics?.quality_count ?? 0} 项质量快照`}
              status={dataHealth?.status ?? "empty"}
            />
            <StatusCard
              icon={<Brain size={17} />}
              label="模型路由"
              value={`${models?.models?.length ?? 0} 个模型实例`}
              status={models?.status ?? "empty"}
            />
          </section>
        ) : null}

        {activeNav === "overview" ? (
          <OverviewPage
            portfolio={portfolio}
            watchlists={watchlists}
            recommendations={recommendations}
            risks={risks}
            workflows={workflows}
            models={models}
            modelPreview={modelPreview}
            memories={memories}
            dataHealth={dataHealth}
            dataSyncConfig={dataSyncConfig}
            dataSchedulerStatus={dataSchedulerStatus}
            refreshDataSync={refreshDataSync}
          />
        ) : (
          <DetailPage
            activeNav={activeNav}
            portfolio={portfolio}
            watchlists={watchlists}
            recommendations={recommendations}
            risks={risks}
            workflows={workflows}
            memories={memories}
            dataHealth={dataHealth}
            dataSyncConfig={dataSyncConfig}
            dataSchedulerStatus={dataSchedulerStatus}
            refreshDataSync={refreshDataSync}
            models={models}
            modelPreview={modelPreview}
            refresh={refresh}
            refreshModelPreview={refreshModelPreview}
            taskSchedulerProgress={taskSchedulerProgress}
            taskSchedulerJobs={taskSchedulerJobs}
            taskMonitorLoading={taskMonitorLoading}
            refreshTaskMonitor={refreshTaskMonitor}
            reportWorkflowRunId={reportWorkflowRunId}
          />
        )}
      </section>
      <ChatDock
        chatLines={chatLines}
        message={message}
        setMessage={setMessage}
        sendMessage={sendMessage}
        open={chatOpen}
        setOpen={setChatOpen}
        streaming={chatStreaming}
        status={chatStatus}
      />
    </main>
  );
}

function StatusCard({
  icon,
  label,
  value,
  status,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  status: string;
}) {
  const tone = toneByStatus(status);
  return (
    <article className={`status-card tone-${tone}`}>
      <div className="status-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
      <em>{statusText(status)}</em>
    </article>
  );
}

function OverviewPage({
  portfolio,
  watchlists,
  recommendations,
  risks,
  workflows,
  models,
  modelPreview,
  memories,
  dataHealth,
  dataSyncConfig,
  dataSchedulerStatus,
  refreshDataSync,
}: ConsolePageProps) {
  return (
    <>
      <DataSyncControlPanel
        dataSyncConfig={dataSyncConfig}
        dataSchedulerStatus={dataSchedulerStatus}
        refreshDataSync={refreshDataSync}
      />

      <section className="main-grid">
        <section className="primary-column">
          <PendingActionsPanel
            watchlists={watchlists}
            recommendations={recommendations}
            risks={risks}
          />
          <PortfolioPanel portfolio={portfolio} />
          <RecommendationPanel recommendations={recommendations} />
        </section>

        <aside className="context-column">
          <AgentSummaryPanel workflows={workflows} models={models} modelPreview={modelPreview} />
          <RiskPanel risks={risks} />
          <MemoryPanel memories={memories} />
        </aside>
      </section>

      <section className="bottom-grid">
        <WatchlistPanel watchlists={watchlists} />
        <DataHealthPanel dataHealth={dataHealth} />
      </section>
    </>
  );
}

function DetailPage({
  activeNav,
  portfolio,
  watchlists,
  recommendations,
  risks,
  workflows,
  memories,
  dataHealth,
  dataSyncConfig,
  dataSchedulerStatus,
  refreshDataSync,
  models,
  modelPreview,
  refresh,
  refreshModelPreview,
  taskSchedulerProgress,
  taskSchedulerJobs,
  taskMonitorLoading,
  refreshTaskMonitor,
  reportWorkflowRunId,
}: ConsolePageProps & { activeNav: NavId }) {
  switch (activeNav) {
    case "portfolio":
      return (
        <section className="page-grid">
          <PortfolioPanel portfolio={portfolio} />
          <RiskPanel risks={risks} />
        </section>
      );
    case "watchlist":
      return (
        <section className="page-grid">
          <WatchlistPanel watchlists={watchlists} />
          <MemoryPanel memories={memories} />
          <PendingActionsPanel
            watchlists={watchlists}
            recommendations={recommendations}
            risks={risks}
          />
        </section>
      );
    case "recommendation":
      return <RecommendationPage ownerId={ownerId} />;
    case "risk":
      return (
        <section className="page-grid">
          <RiskPanel risks={risks} />
          <DataHealthPanel dataHealth={dataHealth} />
          <PortfolioPanel portfolio={portfolio} />
        </section>
      );
    case "agent":
      return (
        <section className="page-grid">
          <AgentSummaryPanel workflows={workflows} models={models} modelPreview={modelPreview} />
          <MemoryPanel memories={memories} />
        </section>
      );
    case "report":
      return <ReportPage ownerId={ownerId} initialWorkflowRunId={reportWorkflowRunId} />;
    case "memory":
      return (
        <section className="page-grid">
          <MemoryPanel memories={memories} />
          <WatchlistPanel watchlists={watchlists} />
          <RecommendationPanel recommendations={recommendations} />
        </section>
      );
    case "data":
      return (
        <section className="page-grid">
          <DataSyncControlPanel
            dataSyncConfig={dataSyncConfig}
            dataSchedulerStatus={dataSchedulerStatus}
            refreshDataSync={refreshDataSync}
          />
          <DataHealthPanel dataHealth={dataHealth} />
          <RiskPanel risks={risks} />
        </section>
      );
    case "tasks":
      return (
        <TaskMonitorPage
          taskSchedulerProgress={taskSchedulerProgress}
          taskSchedulerJobs={taskSchedulerJobs}
          dataSyncConfig={dataSyncConfig}
          dataSchedulerStatus={dataSchedulerStatus}
          taskMonitorLoading={taskMonitorLoading}
          refreshTaskMonitor={refreshTaskMonitor}
        />
      );
    case "model":
      return (
        <section className="page-grid">
          <OpenAICompatibleModelPanel
            models={models}
            modelPreview={modelPreview}
            refresh={refresh}
            refreshModelPreview={refreshModelPreview}
          />
          <AgentSummaryPanel workflows={workflows} models={models} modelPreview={modelPreview} />
          <DataHealthPanel dataHealth={dataHealth} />
        </section>
      );
    default:
      return null;
  }
}

type ConsolePageProps = {
  portfolio: Record<string, any> | undefined;
  watchlists: Record<string, any> | undefined;
  recommendations: Record<string, any> | undefined;
  risks: Record<string, any> | undefined;
  workflows: Record<string, any> | undefined;
  memories: Record<string, any> | undefined;
  dataHealth: Record<string, any> | undefined;
  dataSyncConfig?: Record<string, any> | null;
  dataSchedulerStatus?: Record<string, any> | null;
  refreshDataSync?: () => Promise<void>;
  models?: Record<string, any> | undefined;
  modelPreview?: Record<string, any> | null;
  refresh?: () => Promise<void>;
  refreshModelPreview?: () => Promise<void>;
  taskSchedulerProgress?: Record<string, any> | null;
  taskSchedulerJobs?: Record<string, any> | null;
  taskMonitorLoading?: boolean;
  refreshTaskMonitor?: (silent?: boolean) => Promise<void>;
  reportWorkflowRunId?: string | null;
};

function PendingActionsPanel({
  watchlists,
  recommendations,
  risks,
}: Pick<ConsolePageProps, "watchlists" | "recommendations" | "risks">) {
  return (
    <Panel
      title="今日待处理建议"
      subtitle="按风险、推荐和观察池触发聚合，不展示隐藏推理链"
      icon={<CircleDot size={16} />}
    >
      <div className="action-stack">
        <ActionRow
          tone="red"
          title="先处理高风险持仓"
          detail={`${risks?.metrics?.high_severity_count ?? 0} 条高优先级触发，需查看风险反驳和数据质量。`}
          meta="需要人工确认"
        />
        <ActionRow
          tone="green"
          title="复核最新推荐排序"
          detail={`${recommendations?.metrics?.recommendation_count ?? 0} 条候选推荐，可进入观察池或等待触发。`}
          meta="A 股 / 数字货币分链路"
        />
        <ActionRow
          tone="amber"
          title="沉淀观察池每日原因"
          detail={`${watchlists?.metrics?.active_count ?? 0} 个活跃观察项，继续记录关注原因和失效条件。`}
          meta="写入 Finance Memory"
        />
      </div>
    </Panel>
  );
}

function PortfolioPanel({ portfolio }: Pick<ConsolePageProps, "portfolio">) {
  return (
    <Panel
      title="持仓风险矩阵"
      subtitle="组合事实来自数据库，建议由 Agent/Workflow 生成"
      icon={<Wallet size={16} />}
    >
      <div className="portfolio-strip">
        <MetricBlock label="活跃持仓" value={portfolio?.metrics?.position_count ?? 0} />
        <MetricBlock label="盈利持仓" value={portfolio?.metrics?.positive_position_count ?? 0} />
        <MetricBlock label="亏损持仓" value={portfolio?.metrics?.negative_position_count ?? 0} />
        <MetricBlock label="风险画像" value={portfolio?.metrics?.risk_profile ?? "未配置"} />
      </div>
      <DataTable
        columns={["标的", "市场", "方向", "市值", "浮盈亏", "权重"]}
        rows={(portfolio?.positions ?? []).slice(0, 10).map((item: any) => [
          item.symbol,
          item.market,
          item.side,
          item.market_value ?? "-",
          item.unrealized_pnl ?? "-",
          item.portfolio_weight ?? "-",
        ])}
        emptyText="暂无持仓数据"
      />
    </Panel>
  );
}

function RecommendationPanel({
  recommendations,
}: Pick<ConsolePageProps, "recommendations">) {
  return (
    <Panel
      title="推荐排序"
      subtitle="推荐结果需要能跳转到评分、信号、风险和证据"
      icon={<BarChart3 size={16} />}
    >
      <DataTable
        columns={["排名", "标的", "市场", "动作", "评分", "置信度"]}
        rows={(recommendations?.recommendations ?? []).slice(0, 12).map((item: any) => [
          item.rank,
          `${item.symbol} ${item.name ?? ""}`,
          item.market,
          item.action,
          item.total_score,
          item.confidence,
        ])}
        emptyText="暂无可用推荐运行"
      />
    </Panel>
  );
}

function AgentSummaryPanel({
  workflows,
  models,
  modelPreview,
}: Pick<ConsolePageProps, "workflows" | "models" | "modelPreview">) {
  const previewRoutes = Array.isArray(modelPreview?.data?.routes) ? modelPreview.data.routes : [];
  const agentModelNodes = buildAgentModelNodes(models, previewRoutes);

  return (
    <Panel
      title="Agent 决策摘要"
      subtitle="圆桌观点、模型选择和复核状态"
      icon={<Brain size={16} />}
    >
      <div className="agent-summary">
        <div className="agent-node">
          <CheckCircle2 size={16} />
          <span>Workflow 可用数</span>
          <strong>{workflows?.available?.length ?? 0}</strong>
        </div>
        <div className="agent-node">
          <Clock3 size={16} />
          <span>最近运行</span>
          <strong>{workflows?.metrics?.recent_count ?? 0}</strong>
        </div>
        <div className="agent-node">
          <AlertTriangle size={16} />
          <span>失败运行</span>
          <strong>{workflows?.metrics?.failed_count ?? 0}</strong>
        </div>
      </div>
      <div className="agent-model-summary">
        {agentModelNodes.map((node) => (
          <div className="agent-node agent-node-model" key={node.role}>
            <ServerCog size={16} />
            <span>{node.label}</span>
            <strong>{node.modelName}</strong>
            <p>
              {node.detail}
              {" · "}
              {node.status}
            </p>
          </div>
        ))}
      </div>
      <DataTable
        columns={["Agent 节点", "模型 Key", "模型名", "提供方", "状态"]}
        rows={agentModelNodes.map((node) => [
          node.label,
          node.modelKey || "-",
          node.modelName,
          node.provider,
          node.status,
        ])}
        emptyText="暂无 Agent 模型路由"
      />
      <Timeline
        items={(workflows?.runs ?? []).slice(0, 8).map((item: any) => ({
          title: item.workflow_type,
          meta: item.status,
          detail: item.started_at,
        }))}
        emptyText="暂无 Workflow 审计"
      />
    </Panel>
  );
}

function RiskPanel({ risks }: Pick<ConsolePageProps, "risks">) {
  return (
    <Panel
      title="风险反驳"
      subtitle="高风险动作必须先经过复核"
      icon={<ShieldAlert size={16} />}
    >
      <Timeline
        items={(risks?.triggers ?? []).slice(0, 8).map((item: any) => ({
          title: item.trigger_type,
          meta: item.severity,
          detail: item.payload?.reason ?? item.requested_workflow_type,
        }))}
        emptyText="暂无触发事件"
      />
    </Panel>
  );
}

function MemoryPanel({ memories }: Pick<ConsolePageProps, "memories">) {
  return (
    <Panel
      title="Finance Memory"
      subtitle="历史建议、用户反馈和复盘结果"
      icon={<GitBranch size={16} />}
    >
      <Timeline
        items={(memories?.memories ?? []).slice(0, 8).map((item: any) => ({
          title: item.memory_type,
          meta: item.status,
          detail: item.content,
        }))}
        emptyText="暂无金融记忆"
      />
    </Panel>
  );
}

function WatchlistPanel({ watchlists }: Pick<ConsolePageProps, "watchlists">) {
  const pools = Array.isArray(watchlists?.pools) ? watchlists.pools : [];
  return (
    <Panel title="观察池" subtitle="入池原因、每日关注原因、启动/失效条件" icon={<ListChecks size={16} />}>
      {pools.length ? (
        <div className="watchlist-pool-grid">
          {pools.map((pool: any) => (
            <div className="watchlist-pool-card" key={pool.key}>
              <span>{pool.label}</span>
              <strong>{pool.count ?? 0}</strong>
              <p>{pool.description}</p>
            </div>
          ))}
        </div>
      ) : null}
      <DataTable
        columns={["池子", "标的", "市场", "风险", "来源", "关注原因"]}
        rows={(watchlists?.items ?? []).slice(0, 12).map((item: any) => [
          item.pool_label ?? item.pool ?? "-",
          item.symbol,
          item.market,
          item.risk_level ?? "-",
          item.source_type,
          item.reason,
        ])}
        emptyText="暂无观察池条目"
      />
    </Panel>
  );
}

function DataHealthPanel({ dataHealth }: Pick<ConsolePageProps, "dataHealth">) {
  return (
    <Panel title="数据健康" subtitle="采集、清洗、缺口和过期状态" icon={<Database size={16} />}>
      <Timeline
        items={(dataHealth?.items ?? []).slice(0, 10).map((item: any) => ({
          title: `${item.market} / ${item.data_domain}`,
          meta: item.status,
          detail: `${item.provider}，问题数 ${item.issue_count}`,
        }))}
        emptyText="暂无数据质量快照"
      />
    </Panel>
  );
}

function TaskMonitorPage({
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
    waiting: model.items.filter((item) => item.status === "waiting").length,
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
                    <em>{formatPercent(task.progressRatio)}</em>
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
                    <TaskProgressBar ratio={task.progressRatio} tone={task.tone} />
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
          <span>{formatPercent(task.progressRatio)}</span>
        </div>
        <TaskProgressBar ratio={task.progressRatio} tone="blue" />
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
              return (
                <article key={state.sourceKey} className={`source-rate-item tone-${tone}`}>
                  <span className="source-rate-icon">
                    {tone === "red" ? <AlertTriangle size={18} /> : <Wifi size={18} />}
                  </span>
                  <div className="source-rate-main">
                    <div className="source-rate-head">
                      <strong>{state.sourceKey || "unknown_source"}</strong>
                      <em>{sourceRateLabel(state)}</em>
                    </div>
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
        <MetricBlock label="任务配置并发" value={workerValue} />
        <MetricBlock label="源有效并发" value={effectiveSourceConcurrency} />
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

function sourceRateErrorCount(state: TaskMonitorSourceRateState): number {
  return state.failureCount + state.timeoutCount + state.disconnectCount + state.rateLimitedCount;
}

function sourceRateTone(state: TaskMonitorSourceRateState): "green" | "amber" | "red" {
  if (state.rateLimitedCount > 0 || state.failureRate >= 0.2) {
    return "red";
  }
  if (state.timeoutCount > 0 || state.disconnectCount > 0 || state.failureRate >= 0.1) {
    return "amber";
  }
  return "green";
}

function sourceRateLabel(state: TaskMonitorSourceRateState): string {
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

function TaskProgressBar({ ratio, tone }: { ratio: number; tone: string }) {
  return (
    <div className={`task-progress-bar tone-${tone}`} aria-label={`进度 ${formatPercent(ratio)}`}>
      <span style={{ width: formatPercent(ratio) }} />
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

function clampSchedulerConcurrency(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 4;
  }
  return Math.min(16, Math.max(1, Math.round(parsed)));
}

function DataSyncControlPanel({
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

function ChatDock({
  chatLines,
  message,
  setMessage,
  sendMessage,
  open,
  setOpen,
  streaming,
  status,
}: {
  chatLines: ChatLine[];
  message: string;
  setMessage: React.Dispatch<React.SetStateAction<string>>;
  sendMessage: () => Promise<void>;
  open: boolean;
  setOpen: React.Dispatch<React.SetStateAction<boolean>>;
  streaming: boolean;
  status: string;
}) {
  const logRef = React.useRef<HTMLDivElement | null>(null);
  const canSend = message.trim().length > 0 && !streaming;
  const activeAssistantId = [...chatLines]
    .reverse()
    .find((line) => line.role === "assistant")?.id;

  React.useEffect(() => {
    if (!open) {
      return;
    }
    logRef.current?.scrollTo({
      top: logRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [chatLines, open, status]);

  return (
    <aside className={open ? "chat-dock is-open" : "chat-dock"} aria-live="polite">
      {open ? (
        <section className="chat-window" role="dialog" aria-label="金融 Agent 聊天窗口">
          <header className="chat-window-head">
            <div className="chat-title">
              <span className="chat-avatar">
                <MessageSquareText size={18} />
              </span>
              <div>
                <strong>金融 Agent</strong>
                <span>{streaming ? "正在生成回复" : "随时待命"}</span>
              </div>
            </div>
            <div className="chat-head-actions">
              <span className={streaming ? "chat-status is-active" : "chat-status"}>{status}</span>
              <button
                className="icon-button"
                type="button"
                aria-label="收起聊天窗口"
                title="收起"
                onClick={() => setOpen(false)}
              >
                <X size={16} />
              </button>
            </div>
          </header>

          <div className="chat-log" ref={logRef}>
            {chatLines.map((line) => {
              const isLastTypingAssistant =
                streaming &&
                line.role === "assistant" &&
                line.id === activeAssistantId;
              return (
              <div
                key={line.id}
                className={`chat-message chat-message-${line.role}${
                  isLastTypingAssistant ? " is-typing" : ""
                }`}
              >
                <span>
                  {line.role === "user"
                    ? "你"
                    : line.role === "error"
                      ? "错误"
                      : line.role === "status"
                        ? (line.label ?? "流程")
                        : "Agent"}
                </span>
                <p>
                  {line.content ? (
                    <>
                      {line.content}
                      {isLastTypingAssistant ? (
                        <span className="typewriter-cursor" aria-hidden="true" />
                      ) : null}
                    </>
                  ) :
                    (streaming && line.role === "assistant" ? (
                      <span className="typing-dots" aria-label="正在生成">
                        <i />
                        <i />
                        <i />
                      </span>
                    ) : null)}
                </p>
              </div>
              );
            })}
          </div>

          <form
            className="chat-compose"
            onSubmit={(event) => {
              event.preventDefault();
              if (canSend) {
                void sendMessage();
              }
            }}
          >
            <input
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="问：有哪些适合买入？"
              disabled={streaming}
            />
            <button className="button button-primary" type="submit" disabled={!canSend}>
              {streaming ? "生成中" : "发送"}
              <ChevronRight size={15} />
            </button>
          </form>
        </section>
      ) : (
        <button className="chat-launcher" type="button" onClick={() => setOpen(true)}>
          <MessageSquareText size={18} />
          <span>Agent</span>
        </button>
      )}
    </aside>
  );
}

function OpenAICompatibleModelPanel({
  models,
  modelPreview,
  refresh,
  refreshModelPreview,
}: Pick<ConsolePageProps, "models" | "modelPreview" | "refresh" | "refreshModelPreview">) {
  const routeItems = Array.isArray(models?.routes) ? models.routes : [];
  const endpoint = React.useMemo(() => summarizeOpenAICompatibleConfig(models), [models]);
  const endpointModelItems = endpoint.models;
  const providerItems = Array.isArray(models?.providers) ? models.providers : [];
  const providerByKey = React.useMemo(
    () => {
      const items = new Map<string, any>();
      providerItems.forEach((provider: any) => {
        if (provider.provider_key) {
          items.set(provider.provider_key, provider);
        }
      });
      return items;
    },
    [providerItems],
  );
  const primaryRoute = findRoute(routeItems, "primary_financial_analyst");
  const reviewRoute = findRoute(routeItems, "high_risk_reviewer");
  const previewRoutes = Array.isArray(modelPreview?.data?.routes) ? modelPreview.data.routes : [];
  const previewPrimaryRoute = findRoute(previewRoutes, "primary_financial_analyst");
  const previewReviewRoute = findRoute(previewRoutes, "high_risk_reviewer");
  const [endpointName, setEndpointName] = React.useState(endpoint.displayName);
  const [baseUrl, setBaseUrl] = React.useState(endpoint.baseUrl);
  const [apiKey, setApiKey] = React.useState("");
  const [apiKeyIsPreview, setApiKeyIsPreview] = React.useState(false);
  const [apiKeyVisible, setApiKeyVisible] = React.useState(false);
  const [secretProviderKey, setSecretProviderKey] = React.useState(endpoint.providerKey);
  const [editingModelKey, setEditingModelKey] = React.useState<string | null>(null);
  const [modelKey, setModelKey] = React.useState("");
  const [modelName, setModelName] = React.useState("");
  const [modelRole, setModelRole] = React.useState("primary_financial_analyst");
  const [primaryModelKey, setPrimaryModelKey] = React.useState("");
  const [reviewModelKey, setReviewModelKey] = React.useState("");
  const [saveStatus, setSaveStatus] = React.useState("未保存");
  const [isSaving, setIsSaving] = React.useState(false);
  const [isTestingConnectivity, setIsTestingConnectivity] = React.useState(false);
  const normalizedModelKey = modelKey.trim();
  const normalizedModelName = modelName.trim();
  const activeProviderKey = normalizedModelKey
    ? openAICompatibleProviderKeyForModel(normalizedModelKey)
    : endpoint.providerKey;
  const hasSavedApiKey = Boolean(apiKey.trim()) || apiKeyIsPreview;
  const endpointReady = Boolean(baseUrl.trim() && (hasSavedApiKey || apiKey.trim()));
  const canSaveEndpoint = Boolean(endpointName.trim() && baseUrl.trim() && (hasSavedApiKey || apiKey.trim()));
  const canTestConnectivity = Boolean(normalizedModelKey && baseUrl.trim() && (hasSavedApiKey || apiKey.trim()));
  const canSaveInstance = Boolean(normalizedModelKey && normalizedModelName);
  const canSaveRoutes = Boolean(primaryModelKey && reviewModelKey);

  React.useEffect(() => {
    if (editingModelKey) {
      return;
    }
    const firstModel = endpointModelItems[0];
    setModelKey(firstModel?.model_key ?? "");
    setModelName(firstModel?.model_name ?? "");
    setModelRole(firstModel?.role ?? "primary_financial_analyst");
    if (firstModel) {
      applyEndpointForm(providerEndpointForModel(firstModel));
      return;
    }
    syncEndpointFromSavedProvider();
  }, [editingModelKey, endpointModelItems[0]?.model_key, providerByKey]);

  React.useEffect(() => {
    const modelKeys = new Set(endpointModelItems.map((item: any) => item.model_key));
    const pickModel = (route: any, fallbackIndex: number) => {
      if (route?.model_key && modelKeys.has(route.model_key)) {
        return route.model_key;
      }
      return endpointModelItems[fallbackIndex]?.model_key ?? endpointModelItems[0]?.model_key ?? "";
    };
    setPrimaryModelKey(pickModel(primaryRoute, 0));
    setReviewModelKey(pickModel(reviewRoute, 1));
  }, [primaryRoute?.model_key, reviewRoute?.model_key, endpointModelItems.length]);

  const afterSave = async (result: Record<string, any>, message: string) => {
    if (result.status !== "ok") {
      setSaveStatus(result.message ?? "保存失败");
      return false;
    }
    setSaveStatus(message);
    await refresh?.();
    await refreshModelPreview?.();
    return true;
  };

  const providerEndpointForModel = (item: any) => {
    const modelProviderKey = item?.provider_key ?? "";
    const provider =
      providerByKey.get(modelProviderKey) ??
      providerByKey.get(openAICompatibleProviderKeyForModel(item?.model_key ?? "")) ??
      null;
    if (!provider) {
      return {
        displayName: item?.model_name ? `${item.model_name} 接入` : OPENAI_COMPATIBLE_PROVIDER_NAME,
        baseUrl: "",
        apiKeyPreview: "",
        apiKeyConfigured: false,
        secretProviderKey: activeProviderKey,
      };
    }
    return {
      displayName: provider.provider_name ?? OPENAI_COMPATIBLE_PROVIDER_NAME,
      baseUrl: provider.base_url ?? "",
      apiKeyPreview: provider.api_key ?? "",
      apiKeyConfigured:
        provider.api_key_configured === true ||
        (Boolean(provider.api_key) && provider.api_key !== "***"),
      secretProviderKey: provider.provider_key ?? activeProviderKey,
    };
  };

  const applyEndpointForm = (data: {
    displayName: string;
    baseUrl: string;
    apiKeyPreview: string;
    apiKeyConfigured: boolean;
    secretProviderKey: string;
  }) => {
    setEndpointName(data.displayName);
    setBaseUrl(data.baseUrl);
    setApiKey(data.apiKeyPreview);
    setApiKeyIsPreview(Boolean(data.apiKeyPreview));
    setApiKeyVisible(false);
    setSecretProviderKey(data.secretProviderKey);
  };

  const saveEndpoint = async (message = "接入端点已保存") => {
    if (!endpointName.trim()) {
      setSaveStatus("请填写接入名称");
      return false;
    }
    if (!baseUrl.trim()) {
      setSaveStatus("请填写 Base URL");
      return false;
    }
    if (!hasSavedApiKey && !apiKey.trim()) {
      setSaveStatus("请填写 API Key");
      return false;
    }
    const normalizedApiKey = apiKeyIsPreview ? null : apiKey.trim() || null;
    const result = await saveModelProvider(activeProviderKey, {
      provider_vendor: "openai_compatible",
      provider_name: endpointName.trim() || OPENAI_COMPATIBLE_PROVIDER_NAME,
      base_url: baseUrl.trim() || null,
      api_key: normalizedApiKey,
      timeout_seconds: 30,
      is_enabled: true,
      is_default: true,
    });
    return afterSave(result, message);
  };

  const revealApiKey = async () => {
    if (apiKeyVisible) {
      setApiKeyVisible(false);
      return;
    }
    if (apiKey.trim() && !apiKeyIsPreview) {
      setApiKeyVisible(true);
      return;
    }
    const result = await revealModelProviderSecret(secretProviderKey || activeProviderKey);
    if (result.status !== "ok") {
      setSaveStatus(result.message ?? "读取 API Key 失败");
      return;
    }
    const revealed = result.data?.api_key ?? "";
    if (!revealed) {
      setSaveStatus("当前接入还没有保存 API Key");
      return;
    }
    setApiKey(revealed);
    setApiKeyIsPreview(false);
    setApiKeyVisible(true);
    setSaveStatus("API Key 已解密显示");
  };

  const testConnectivity = async () => {
    if (!canTestConnectivity) {
      setSaveStatus("请先填写模型 ID、Base URL 和 API Key");
      return;
    }
    setIsTestingConnectivity(true);
    setSaveStatus("正在测试连通性...");
    try {
      const result = await testModelProviderConnectivity(
        buildModelProviderConnectivityPayload({
          providerKey: activeProviderKey,
          modelKey: normalizedModelKey,
          modelName: normalizedModelName,
          baseUrl,
          apiKey,
          apiKeyIsPreview,
        }),
      );
      if (result.status === "ok") {
        const latency = result.data?.latency_ms;
        const httpStatus = result.data?.http_status;
        setSaveStatus(`连通性正常 · HTTP ${httpStatus ?? 200} · ${latency ?? "-"}ms`);
        return;
      }
      setSaveStatus(result.message ?? "连通性测试失败");
    } finally {
      setIsTestingConnectivity(false);
    }
  };

  const syncEndpointFromSavedProvider = () => {
    applyEndpointForm({
      ...endpoint,
      secretProviderKey: endpoint.providerKey,
    });
  };

  const saveInstance = async () => {
    if (!(await saveEndpoint("接入端点已同步"))) {
      return;
    }
    if (!canSaveInstance) {
      setSaveStatus("请填写模型 ID 和显示名称");
      return;
    }
    const result = await saveModelInstance(normalizedModelKey, {
      provider_key: activeProviderKey,
      model_name: normalizedModelName,
      model_type: "llm",
      role: modelRole,
      route_priority: modelRole === "primary_financial_analyst" ? 120 : 100,
      timeout_seconds: 30,
      is_enabled: true,
    });
    if (result.status === "ok") {
      if (modelRole === "high_risk_reviewer") {
        setReviewModelKey(normalizedModelKey);
      } else {
        setPrimaryModelKey(normalizedModelKey);
      }
    }
    const ok = await afterSave(result, editingModelKey ? "模型已更新" : "模型已新增");
    if (ok) {
      setEditingModelKey(normalizedModelKey);
    }
  };

  const resetModelEditor = () => {
    setEditingModelKey(null);
    setModelKey("");
    setModelName("");
    setModelRole("primary_financial_analyst");
    applyEndpointForm({
      displayName: OPENAI_COMPATIBLE_PROVIDER_NAME,
      baseUrl: "",
      apiKeyPreview: "",
      apiKeyConfigured: false,
      secretProviderKey: "",
    });
    setSaveStatus("准备新增模型");
  };

  const editModel = (item: any) => {
    setEditingModelKey(item.model_key ?? null);
    setModelKey(item.model_key ?? "");
    setModelName(item.model_name ?? "");
    setModelRole(item.role ?? "primary_financial_analyst");
    applyEndpointForm(providerEndpointForModel(item));
    setSaveStatus("正在编辑模型");
  };

  const deleteModel = async (item: any) => {
    const targetKey = item.model_key ?? "";
    if (!targetKey) {
      return;
    }
    if (!window.confirm(`停用模型 ${item.model_name ?? targetKey}？`)) {
      return;
    }
    const result = await deleteModelInstance(targetKey);
    if (result.status === "ok") {
      if (editingModelKey === targetKey) {
        resetModelEditor();
      }
      const fallback = endpointModelItems.find((model: any) => model.model_key !== targetKey)?.model_key ?? "";
      if (primaryModelKey === targetKey) {
        setPrimaryModelKey(fallback);
      }
      if (reviewModelKey === targetKey) {
        setReviewModelKey(fallback);
      }
    }
    await afterSave(result, "模型已停用");
  };

  const saveRoutes = async () => {
    if (!canSaveRoutes) {
      setSaveStatus("请先保存可用于 Agent 的模型");
      return;
    }
    const primaryResult = await saveModelRoute("primary_financial_analyst", {
      workflow_type: "*",
      task: "*",
      model_key: primaryModelKey,
      reason: "Web 控制台切换主分析 Agent 模型。",
      priority: 200,
      is_enabled: true,
    });
    if (primaryResult.status !== "ok") {
      await afterSave(primaryResult, "");
      return;
    }
    const reviewResult = await saveModelRoute("high_risk_reviewer", {
      workflow_type: "*",
      task: "high_risk_review",
      model_key: reviewModelKey,
      reason: "Web 控制台切换高风险复核 Agent 模型。",
      priority: 200,
      is_enabled: true,
    });
    await afterSave(reviewResult, "Agent 默认模型已保存");
  };

  const runSaving = async (action: () => Promise<void>) => {
    setIsSaving(true);
    try {
      await action();
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <Panel
      title="OpenAI 兼容模型"
      subtitle="自定义模型端点、模型目录和 Agent 默认模型"
      icon={<Settings2 size={16} />}
    >
      <div className="openai-model-console">
        <section className="endpoint-panel">
          <div className="model-section-head">
            <div>
              <h3>接入端点</h3>
              <p>{endpointReady ? "已连接" : "待配置"}</p>
            </div>
            <span className={`status-pill tone-${endpointReady ? "green" : "amber"}`}>
              {endpointReady ? "可用" : "缺少配置"}
            </span>
          </div>
          <div className="endpoint-form-grid">
            <label>
              <span>名称</span>
              <input value={endpointName} onChange={(event) => setEndpointName(event.target.value)} />
            </label>
            <label className="endpoint-form-wide">
              <span>Base URL</span>
              <input
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder="https://api.example.com/v1"
              />
            </label>
            <label className="endpoint-form-wide">
              <span>API Key</span>
              <div className="secret-input-row">
                <input
                  value={apiKey}
                  onChange={(event) => {
                    setApiKey(event.target.value);
                    setApiKeyIsPreview(false);
                  }}
                  placeholder={hasSavedApiKey ? "已保存，留空不改" : "sk-..."}
                  type={apiKeyVisible ? "text" : "password"}
                />
                <button
                  className="secret-toggle-button"
                  type="button"
                  onClick={() => void revealApiKey()}
                  disabled={isSaving || (!hasSavedApiKey && !apiKey.trim())}
                  title={apiKeyVisible ? "隐藏 API Key" : "显示 API Key"}
                >
                  {apiKeyVisible ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
            </label>
          </div>
          <div className="model-action-row">
            <button
              className="button button-primary"
              onClick={() => void runSaving(() => saveEndpoint().then(() => undefined))}
              disabled={!canSaveEndpoint || isSaving}
            >
              <Check size={15} />
              保存接入
            </button>
            <button
              className="button button-ghost"
              onClick={() => void testConnectivity()}
              disabled={!canTestConnectivity || isSaving || isTestingConnectivity}
              title="测试当前模型接入端点"
            >
              <Wifi size={15} />
              {isTestingConnectivity ? "测试中" : "测试连通性"}
            </button>
            <span>{saveStatus}</span>
          </div>
        </section>

        <section className="model-library-panel">
          <div className="model-section-head">
            <div>
              <h3>模型列表</h3>
              <p>{endpointModelItems.length} 个可用模型</p>
            </div>
            <button className="button button-ghost" onClick={resetModelEditor}>
              <Plus size={15} />
              新增
            </button>
          </div>
          <div className="model-library-list">
            {endpointModelItems.length > 0 ? (
              endpointModelItems.map((item: any) => (
                <article key={item.model_key} className="openai-model-card">
                  <div>
                    <strong>{item.model_name ?? item.model_key}</strong>
                    <span>{item.model_key}</span>
                  </div>
                  <em>{modelRoleLabel(item.role)}</em>
                  <div className="model-card-actions">
                    <button className="button button-ghost" onClick={() => editModel(item)} title="编辑模型">
                      <Pencil size={14} />
                    </button>
                    <button
                      className="button button-ghost button-danger"
                      onClick={() => void runSaving(() => deleteModel(item))}
                      title="停用模型"
                      disabled={isSaving}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </article>
              ))
            ) : (
              <div className="empty-state">暂无模型</div>
            )}
          </div>
        </section>

        <section className="model-editor-panel">
          <div className="model-section-head">
            <div>
              <h3>{editingModelKey ? "编辑模型" : "新增模型"}</h3>
              <p>{editingModelKey ?? "未选择模型"}</p>
            </div>
          </div>
          <label>
            <span>模型 ID</span>
            <input value={modelKey} onChange={(event) => setModelKey(event.target.value)} placeholder="gpt-4.1" />
          </label>
          <label>
            <span>显示名称</span>
            <input value={modelName} onChange={(event) => setModelName(event.target.value)} placeholder="GPT-4.1" />
          </label>
          <label>
            <span>默认用途</span>
            <select value={modelRole} onChange={(event) => setModelRole(event.target.value)}>
              <option value="primary_financial_analyst">主分析 Agent</option>
              <option value="high_risk_reviewer">高风险复核 Agent</option>
            </select>
          </label>
          <div className="model-action-row">
            <button
              className="button button-primary"
              onClick={() => void runSaving(saveInstance)}
              disabled={!canSaveInstance || isSaving}
            >
              <Check size={15} />
              保存模型
            </button>
            <button className="button button-ghost" onClick={resetModelEditor}>
              <Plus size={15} />
              新建
            </button>
          </div>
        </section>

        <section className="agent-binding-panel">
          <div className="model-section-head">
            <div>
              <h3>Agent 默认模型</h3>
              <p>{canSaveRoutes ? "已选择" : "待选择"}</p>
            </div>
          </div>
          <div className="agent-binding-grid">
            <label>
              <span>主分析 Agent</span>
              <select
                value={primaryModelKey}
                onChange={(event) => setPrimaryModelKey(event.target.value)}
                disabled={!endpointModelItems.length}
              >
                {endpointModelItems.map((item: any) => (
                  <option key={item.model_key} value={item.model_key}>
                    {item.model_name ?? item.model_key}
                  </option>
                ))}
              </select>
              <em>{routeStatusLabel(previewPrimaryRoute ?? primaryRoute)}</em>
            </label>
            <label>
              <span>高风险复核 Agent</span>
              <select
                value={reviewModelKey}
                onChange={(event) => setReviewModelKey(event.target.value)}
                disabled={!endpointModelItems.length}
              >
                {endpointModelItems.map((item: any) => (
                  <option key={item.model_key} value={item.model_key}>
                    {item.model_name ?? item.model_key}
                  </option>
                ))}
              </select>
              <em>{routeStatusLabel(previewReviewRoute ?? reviewRoute)}</em>
            </label>
          </div>
          <div className="model-action-row">
            <button
              className="button button-primary"
              onClick={() => void runSaving(saveRoutes)}
              disabled={!canSaveRoutes || isSaving}
            >
              <Check size={15} />
              保存默认模型
            </button>
          </div>
          <div className="route-preview">
            <strong>当前生效</strong>
            {previewRoutes.length > 0 ? (
              previewRoutes.map((route: any) => (
                <p key={`${route.role}-${route.task}-${route.model_key}`}>
                  {modelRoleLabel(route.role)}
                  {" -> "}
                  {route.model_key} / {routeStatusLabel(route)}
                </p>
              ))
            ) : (
              <p>暂无路由</p>
            )}
          </div>
        </section>
      </div>
    </Panel>
  );
}

function findRoute(routes: any[], role: string) {
  return routes.find((item) => item.role === role && item.is_enabled !== false);
}

function Panel({
  title,
  subtitle,
  icon,
  children,
}: {
  title: string;
  subtitle: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <header className="panel-head">
        <div className="panel-icon">{icon}</div>
        <div>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
      </header>
      {children}
    </section>
  );
}

function ActionRow({
  tone,
  title,
  detail,
  meta,
}: {
  tone: "red" | "green" | "amber";
  title: string;
  detail: string;
  meta: string;
}) {
  return (
    <article className={`action-row action-${tone}`}>
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
      <span>{meta}</span>
    </article>
  );
}

function MetricBlock({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="metric-block">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TaskQueueList({
  title,
  jobs,
  emptyText,
}: {
  title: string;
  jobs: string[];
  emptyText: string;
}) {
  return (
    <div className="scheduler-queue-card">
      <strong>{title}</strong>
      {jobs.length > 0 ? (
        <ul>
          {jobs.slice(0, 12).map((job) => (
            <li key={job}>{job}</li>
          ))}
        </ul>
      ) : (
        <span>{emptyText}</span>
      )}
      {jobs.length > 12 ? <em>还有 {jobs.length - 12} 个任务</em> : null}
    </div>
  );
}

function DataTable({
  columns,
  rows,
  emptyText,
}: {
  columns: string[];
  rows: React.ReactNode[][];
  emptyText: string;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length > 0 ? (
            rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <td key={`${rowIndex}-${cellIndex}`}>{cell}</td>
                ))}
              </tr>
            ))
          ) : (
            <tr>
              <td colSpan={columns.length} className="empty-cell">
                {emptyText}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function Timeline({
  items,
  emptyText,
}: {
  items: Array<{ title: string; meta: string; detail: React.ReactNode }>;
  emptyText: string;
}) {
  if (!items.length) {
    return <div className="empty-state">{emptyText}</div>;
  }
  return (
    <div className="timeline">
      {items.map((item, index) => (
        <article key={`${item.title}-${index}`}>
          <span className="timeline-dot" />
          <div>
            <div className="timeline-title">
              <strong>{item.title}</strong>
              <em>{item.meta}</em>
            </div>
            <p>{item.detail}</p>
          </div>
        </article>
      ))}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
