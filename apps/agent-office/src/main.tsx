import React from "react";
import { createRoot } from "react-dom/client";
import { App as AntApp, ConfigProvider, theme as antdTheme } from "antd";
import {
  Activity,
  BarChart3,
  Brain,
  Database,
  FileText,
  GitBranch,
  LineChart,
  ListChecks,
  RefreshCcw,
  Search,
  ServerCog,
  Settings2,
  ShieldAlert,
  Wallet,
} from "lucide-react";
import "antd/dist/reset.css";
import {
  DashboardSummary,
  loadDataSchedulerJobs,
  loadDataSchedulerProgress,
  loadDataSchedulerStatus,
  loadDataSyncConfig,
  loadDashboardSummary,
  loadModelRoutePreview,
  loadPendingDecisions,
  streamChatMessage,
} from "./api";
import { ChatDock } from "./components/ChatDock";
import { StatusCard } from "./components/consoleCommon";
import type { ChatLine, NavId } from "./consoleTypes";
import { DetailPage } from "./pages/DetailPage";
import { OverviewPage } from "./pages/OverviewPage";
import { startSerialPolling } from "./taskMonitorView";
import "./styles.css";

const ownerId = "default-owner";

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

const navigationGroups: Array<{ label: string; items: typeof navigation }> = [
  { label: "工作台", items: [navigation[0], navigation[1], navigation[2]] },
  { label: "决策中心", items: [navigation[3], navigation[4], navigation[5], navigation[6]] },
  { label: "数据与智能", items: [navigation[7], navigation[8], navigation[9]] },
  { label: "系统", items: [navigation[10]] },
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

const CHAT_TYPEWRITER_INTERVAL_MS = 18;

type PulseBarProps = {
  risks: Record<string, any> | undefined;
  dataSchedulerStatus: Record<string, any> | null;
  pendingDecisionCount: number;
  dataHealth: Record<string, any> | undefined;
  portfolios: Record<string, any> | undefined;
  modelCount: number;
};

function PulseBar({
  risks,
  dataSchedulerStatus,
  pendingDecisionCount,
  dataHealth,
  portfolios,
  modelCount,
}: PulseBarProps) {
  const quotes = Array.isArray(risks?.intraday_quotes) ? risks.intraday_quotes : [];
  const validQuotes = quotes.filter((quote: any) => Number.isFinite(Number(quote.change_percent)));
  const upCount = validQuotes.filter((quote: any) => Number(quote.change_percent) > 0).length;
  const downCount = validQuotes.filter((quote: any) => Number(quote.change_percent) < 0).length;
  const flatCount = validQuotes.length - upCount - downCount;
  const breadthTotal = Math.max(validQuotes.length, 1);
  const schedulerHealth = String(dataSchedulerStatus?.health?.status ?? "unknown");
  const schedulerTone =
    schedulerHealth === "ok" || schedulerHealth === "healthy"
      ? "green"
      : schedulerHealth === "degraded"
        ? "amber"
        : "red";
  const dataHealthStatus = String(dataHealth?.status ?? "empty");
  const dataHealthTone =
    dataHealthStatus === "ok" || dataHealthStatus === "healthy"
      ? "green"
      : dataHealthStatus === "partial" || dataHealthStatus === "degraded"
        ? "amber"
        : "muted";
  const qualityCount = Number(dataHealth?.metrics?.quality_count ?? dataHealth?.items?.length ?? 0);
  const lastQuoteAt = quotes
    .map((quote: any) => String(quote.as_of ?? ""))
    .filter(Boolean)
    .sort()
    .at(-1);
  const positionCount = Number(portfolios?.metrics?.position_count ?? 0);

  return (
    <section className="pulse-bar" aria-label="市场脉搏">
      <div className="pulse-brand">
        <Activity size={15} />
        <strong>市场脉搏</strong>
      </div>
      <div className="pulse-market">
        <span className="pulse-market-label">A 股快照</span>
        {validQuotes.length ? (
          <div className="pulse-breadth">
            <div className="pulse-breadth-bar">
              <span className="pulse-breadth-up" style={{ width: `${(upCount / breadthTotal) * 100}%` }} />
              <span className="pulse-breadth-down" style={{ width: `${(downCount / breadthTotal) * 100}%` }} />
              <span className="pulse-breadth-flat" style={{ flex: 1 }} />
            </div>
            <span className="pulse-counts">
              <span className="up">涨 {upCount}</span>
              <span className="down">跌 {downCount}</span>
              {flatCount ? <span className="flat">平 {flatCount}</span> : null}
            </span>
          </div>
        ) : (
          <span className="pulse-counts flat">等待有效行情快照</span>
        )}
      </div>
      <div className="pulse-items">
        <span className={`pulse-chip tone-${schedulerTone}`}>
          <Database size={12} />
          调度器 <strong>{schedulerHealth}</strong>
        </span>
        <span className={`pulse-chip tone-${dataHealthTone}`}>
          <ServerCog size={12} />
          数据健康 <strong>{qualityCount}</strong> 项
        </span>
        <span className={`pulse-chip ${pendingDecisionCount ? "tone-amber" : ""}`}>
          <ListChecks size={12} />
          待确认决策 <strong>{pendingDecisionCount}</strong>
        </span>
        <span className="pulse-chip">
          <Wallet size={12} />
          持仓 <strong>{positionCount}</strong> 笔
        </span>
        <span className="pulse-chip">
          <Brain size={12} />
          模型 <strong>{modelCount}</strong> 个
        </span>
      </div>
      <span className="pulse-sync">
        {lastQuoteAt ? (
          <>
            <span className="is-fresh">●</span>
            行情同步于 {new Date(lastQuoteAt).toLocaleTimeString("zh-CN", { hour12: false })}
          </>
        ) : (
          "暂无行情快照"
        )}
      </span>
    </section>
  );
}

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
  const [pendingDecisionPayload, setPendingDecisionPayload] = React.useState<Record<string, any> | null>(null);
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
    let firstPoll = true;
    return startSerialPolling(async () => {
      const silent = !firstPoll;
      firstPoll = false;
      await refreshTaskMonitor(silent);
    }, 2000);
  }, [refreshTaskMonitor]);

  const refreshModelPreview = React.useCallback(async () => {
    const preview = await loadModelRoutePreview();
    setModelPreview(preview);
  }, []);

  const refreshPendingDecisions = React.useCallback(async () => {
    try {
      setPendingDecisionPayload(await loadPendingDecisions(ownerId, 40));
    } catch {
      setPendingDecisionPayload(null);
    }
  }, []);

  React.useEffect(() => {
    void refreshPendingDecisions();
  }, [refreshPendingDecisions, summary?.generated_at]);

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
  const schedulerHealth = String(dataSchedulerStatus?.health?.status ?? "unknown");
  const schedulerTone =
    schedulerHealth === "ok" || schedulerHealth === "healthy"
      ? "green"
      : schedulerHealth === "degraded"
        ? "amber"
        : schedulerHealth === "error" || schedulerHealth === "failed"
          ? "red"
          : "muted";
  const pendingDecisionCount = Array.isArray(pendingDecisionPayload?.data?.items)
    ? pendingDecisionPayload.data.items.length
    : 0;

  const appContent = (
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
          {navigationGroups.map((group) => (
            <React.Fragment key={group.label}>
              <span className="nav-group">{group.label}</span>
              {group.items.map((item) => {
                const Icon = item.icon;
                return (
                  <button
                    key={item.id}
                    className={activeNav === item.id ? "nav-item is-active" : "nav-item"}
                    aria-current={activeNav === item.id ? "page" : undefined}
                    title={item.label}
                    onClick={() => setActiveNav(item.id)}
                  >
                    <Icon size={17} />
                    <span>{item.label}</span>
                  </button>
                );
              })}
            </React.Fragment>
          ))}
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
              <span className={`status-pill tone-${schedulerTone}`} title="基础数据调度器健康状态">
                <span className="status-pill-dot" />
                调度器 {schedulerHealth}
              </span>
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
          <PulseBar
            risks={risks}
            dataSchedulerStatus={dataSchedulerStatus}
            pendingDecisionCount={pendingDecisionCount}
            dataHealth={dataHealth}
            portfolios={portfolio}
            modelCount={models?.models?.length ?? 0}
          />
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
            pendingDecisionCount={pendingDecisionCount}
            onNavigate={(nav) => setActiveNav(nav)}
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
            ownerId={ownerId}
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

  return (
    <ConfigProvider
      theme={{
        algorithm: antdTheme.darkAlgorithm,
        token: {
          colorPrimary: "#5b8ff9",
          colorInfo: "#5b8ff9",
          colorSuccess: "#2fd181",
          colorWarning: "#e8a93e",
          colorError: "#f0566a",
          colorBgBase: "#0a0f1b",
          colorBgContainer: "#111827",
          colorBgElevated: "#162032",
          colorBgLayout: "#0a0f1b",
          colorBorder: "#232f45",
          colorBorderSecondary: "#232f45",
          colorText: "#e8eef8",
          colorTextSecondary: "#b6c2d4",
          colorTextTertiary: "#8491a7",
          borderRadius: 6,
          controlHeight: 30,
          fontFamily:
            '"Fira Sans","Microsoft YaHei UI","PingFang SC","Helvetica Neue",Arial,sans-serif',
        },
        components: {
          Table: {
            headerBg: "#162032",
            headerColor: "#8491a7",
            rowHoverBg: "#162032",
            borderColor: "#232f45",
          },
          Tag: { defaultBg: "#1b2739" },
          Modal: { contentBg: "#111827", headerBg: "#111827" },
          Drawer: { colorBgElevated: "#0e1522" },
          Tabs: { inkBarColor: "#5b8ff9", itemSelectedColor: "#cfe0ff" },
          Segmented: {
            itemSelectedBg: "#202e44",
            trackBg: "#111827",
            itemSelectedColor: "#cfe0ff",
          },
        },
      }}
    >
      <AntApp>{appContent}</AntApp>
    </ConfigProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
