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
  Pencil,
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
  DashboardSummary,
  deleteModelInstance,
  loadDataSchedulerStatus,
  loadDataSyncConfig,
  loadDashboardSummary,
  loadModelRoutePreview,
  revealModelProviderSecret,
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

  return (
    <main className="terminal-app">
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

      <section className="workspace">
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
              }}
              disabled={loading}
            >
              <RefreshCcw size={15} />
              {loading ? "刷新中" : "刷新"}
            </button>
          </div>
        </header>

        {error ? <div className="notice notice-red">{error}</div> : null}

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
      return (
        <section className="page-grid">
          <RecommendationPanel recommendations={recommendations} />
          <RiskPanel risks={risks} />
          <AgentSummaryPanel workflows={workflows} models={models} modelPreview={modelPreview} />
        </section>
      );
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
      return (
        <section className="page-grid">
          <ReportPanel workflows={workflows} recommendations={recommendations} risks={risks} />
          <RecommendationPanel recommendations={recommendations} />
          <RiskPanel risks={risks} />
        </section>
      );
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
  return (
    <Panel title="观察池" subtitle="入池原因、每日关注原因、启动/失效条件" icon={<ListChecks size={16} />}>
      <DataTable
        columns={["标的", "市场", "风险", "来源", "关注原因"]}
        rows={(watchlists?.items ?? []).slice(0, 12).map((item: any) => [
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
            {["ashare", "crypto_spot", "crypto_future"].map((market) => (
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

function ReportPanel({
  workflows,
  recommendations,
  risks,
}: Pick<ConsolePageProps, "workflows" | "recommendations" | "risks">) {
  return (
    <Panel title="中文报告工作台" subtitle="汇总推荐、风险反驳和 Workflow 审计" icon={<FileText size={16} />}>
      <div className="portfolio-strip">
        <MetricBlock label="可生成 Workflow" value={workflows?.available?.length ?? 0} />
        <MetricBlock label="推荐条目" value={recommendations?.metrics?.recommendation_count ?? 0} />
        <MetricBlock label="风险触发" value={risks?.metrics?.trigger_count ?? 0} />
        <MetricBlock label="报告状态" value="待生成" />
      </div>
      <Timeline
        items={(workflows?.runs ?? []).slice(0, 6).map((item: any) => ({
          title: `${item.workflow_type} 报告线索`,
          meta: item.status,
          detail: item.started_at,
        }))}
        emptyText="暂无可用报告审计"
      />
    </Panel>
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
