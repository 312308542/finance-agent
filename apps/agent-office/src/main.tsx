import React from "react";
import { createRoot } from "react-dom/client";
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
import {
  DashboardSummary,
  loadDataSchedulerJobs,
  loadDataSchedulerProgress,
  loadDataSchedulerStatus,
  loadDataSyncConfig,
  loadDashboardSummary,
  loadModelRoutePreview,
  streamChatMessage,
} from "./api";
import { ChatDock } from "./components/ChatDock";
import { StatusCard } from "./components/consoleCommon";
import type { ChatLine, NavId } from "./consoleTypes";
import { DetailPage } from "./pages/DetailPage";
import { OverviewPage } from "./pages/OverviewPage";
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
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
