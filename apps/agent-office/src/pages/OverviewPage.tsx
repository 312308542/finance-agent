import { AlertTriangle, BarChart3, Bell, Brain, CheckCircle2, CircleDot, ClipboardCheck, Clock3, Database, Eye, GitBranch, ListChecks, ServerCog, ShieldAlert, Wallet } from "lucide-react";
import { buildAgentModelNodes } from "../agentModelRoutes";
import type { ConsolePageProps, NavId } from "../consoleTypes";
import { ActionRow, DataTable, MetricBlock, Panel, Timeline } from "../components/consoleCommon";
import { DataSyncControlPanel } from "./DataSyncControlPanel";

type OverviewExtraProps = {
  pendingDecisionCount?: number;
  onNavigate?: (nav: NavId) => void;
};

export function OverviewPage({
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
  pendingDecisionCount = 0,
  onNavigate,
}: ConsolePageProps & OverviewExtraProps) {
  return (
    <>
      <HeroKpiRow
        portfolio={portfolio}
        watchlists={watchlists}
        recommendations={recommendations}
        risks={risks}
        dataHealth={dataHealth}
        pendingDecisionCount={pendingDecisionCount}
        onNavigate={onNavigate}
      />

      <section className="home-workspace">
        <TodayFeed
          risks={risks}
          workflows={workflows}
          memories={memories}
          portfolio={portfolio}
          onNavigate={onNavigate}
        />

        <aside className="home-rail">
          <PortfolioSnapshotPanel portfolio={portfolio} onNavigate={onNavigate} />
          <AgentMiniPanel workflows={workflows} models={models} modelPreview={modelPreview} onNavigate={onNavigate} />
        </aside>
      </section>

      <section className="bottom-grid">
        <WatchlistPanel watchlists={watchlists} />
        <DataHealthPanel dataHealth={dataHealth} />
      </section>

      <DataSyncControlPanel
        dataSyncConfig={dataSyncConfig}
        dataSchedulerStatus={dataSchedulerStatus}
        refreshDataSync={refreshDataSync}
      />
    </>
  );
}

/* ---------------- 驾驶舱 KPI ---------------- */

function HeroKpiRow({
  portfolio,
  watchlists,
  recommendations,
  risks,
  dataHealth,
  pendingDecisionCount = 0,
  onNavigate,
}: Pick<ConsolePageProps, "portfolio" | "watchlists" | "recommendations" | "risks" | "dataHealth"> & OverviewExtraProps) {
  const positionCount = portfolio?.metrics?.position_count ?? 0;
  const positiveCount = portfolio?.metrics?.positive_position_count ?? 0;
  const negativeCount = portfolio?.metrics?.negative_position_count ?? 0;
  const riskProfile = String(portfolio?.metrics?.risk_profile ?? "未配置");
  const riskProfileTone = riskProfileToneFor(riskProfile);

  const recommendationCount = recommendations?.metrics?.recommendation_count ?? 0;
  const buyCount = recommendations?.metrics?.buy_count ?? 0;
  const watchCount = recommendations?.metrics?.watch_count ?? 0;

  const watchlistCount = watchlists?.metrics?.active_count ?? 0;
  const watchlistHighRisk = watchlists?.metrics?.high_risk_count ?? 0;

  const highSeverityCount = risks?.metrics?.high_severity_count ?? 0;
  const riskFindingCount = risks?.metrics?.risk_finding_count ?? 0;

  const qualityCount = Number(dataHealth?.metrics?.quality_count ?? dataHealth?.items?.length ?? 0);
  const dataIssueCount = risks?.metrics?.data_issue_count ?? 0;
  const dataHealthOk = dataHealth?.status === "ok" || dataHealth?.status === "healthy";

  const cards = [
    {
      key: "risk-profile",
      label: "组合风险等级",
      icon: <ShieldAlert size={16} />,
      value: riskProfile,
      isText: true,
      tone: riskProfileTone,
      sub: positionCount ? `${positionCount} 笔持仓 · 盈 ${positiveCount} 亏 ${negativeCount}` : "配置组合后自动评估",
      nav: "portfolio" as NavId,
    },
    {
      key: "pending",
      label: "待确认决策",
      icon: <ListChecks size={16} />,
      value: String(pendingDecisionCount),
      tone: pendingDecisionCount > 0 ? "amber" : "green",
      sub: pendingDecisionCount > 0 ? "等待人工确认与反馈" : "暂无待处理决策",
      nav: "recommendation" as NavId,
    },
    {
      key: "recommendation",
      label: "今日推荐",
      icon: <BarChart3 size={16} />,
      value: String(recommendationCount),
      tone: "blue",
      sub: `${buyCount} 候选买入 · ${watchCount} 建议观察`,
      nav: "recommendation" as NavId,
    },
    {
      key: "watchlist",
      label: "活跃观察项",
      icon: <Eye size={16} />,
      value: String(watchlistCount),
      tone: "cyan",
      sub: watchlistHighRisk ? `${watchlistHighRisk} 项高风险观察` : "持续跟踪候选标的",
      nav: "watchlist" as NavId,
    },
    {
      key: "high-risk",
      label: "高风险触发",
      icon: <AlertTriangle size={16} />,
      value: String(highSeverityCount),
      tone: highSeverityCount > 0 ? "red" : "green",
      sub: `${riskFindingCount} 项风险发现`,
      nav: "risk" as NavId,
    },
    {
      key: "data-health",
      label: "数据健康",
      icon: <Database size={16} />,
      value: String(qualityCount),
      tone: dataHealthOk ? "green" : "amber",
      sub: dataIssueCount ? `${dataIssueCount} 个数据问题待查` : "数据链路运行正常",
      nav: "data" as NavId,
    },
  ];

  return (
    <section className="hero-kpi-row" aria-label="今日关键指标">
      {cards.map((card) => (
        <button
          key={card.key}
          type="button"
          className={`kpi-card tone-${card.tone}`}
          onClick={() => onNavigate?.(card.nav)}
        >
          <span className="kpi-card-head">
            <span className="kpi-card-icon">{card.icon}</span>
            <span className="kpi-card-label">{card.label}</span>
          </span>
          <strong className={card.isText ? "kpi-card-value is-text" : "kpi-card-value"}>{card.value}</strong>
          <span className="kpi-card-sub">{card.sub}</span>
        </button>
      ))}
    </section>
  );
}

function riskProfileToneFor(profile: string): string {
  if (/低|保守/.test(profile)) {
    return "green";
  }
  if (/高|激进/.test(profile)) {
    return "red";
  }
  if (/中|平衡|稳健/.test(profile)) {
    return "amber";
  }
  return "blue";
}

/* ---------------- 叙事流 Feed ---------------- */

type FeedItem = {
  key: string;
  icon: React.ReactNode;
  tone: string;
  title: string;
  detail: string;
  time: string;
  nav: NavId;
};

function TodayFeed({
  risks,
  workflows,
  memories,
  portfolio,
  onNavigate,
}: Pick<ConsolePageProps, "risks" | "workflows" | "memories" | "portfolio"> & OverviewExtraProps) {
  const items: FeedItem[] = [];

  for (const trigger of Array.isArray(risks?.triggers) ? risks.triggers : []) {
    items.push({
      key: `trigger-${trigger.trigger_event_id ?? Math.random()}`,
      icon: <ShieldAlert size={15} />,
      tone: severityTone(String(trigger.severity ?? "")),
      title: `${String(trigger.trigger_type ?? "触发事件")}${trigger.asset_id ? ` · ${trigger.asset_id}` : ""}`,
      detail: String(trigger.payload?.reason ?? trigger.requested_workflow_type ?? "等待 Agent 处理"),
      time: String(trigger.triggered_at ?? ""),
      nav: "risk",
    });
  }
  for (const alert of Array.isArray(risks?.alerts) ? risks.alerts : []) {
    items.push({
      key: `alert-${alert.alert_id ?? Math.random()}`,
      icon: <Bell size={15} />,
      tone: severityTone(String(alert.severity ?? "")),
      title: `${String(alert.alert_type ?? "监控提醒")}${alert.asset_id ? ` · ${alert.asset_id}` : ""}`,
      detail: String(alert.trigger_condition ?? alert.payload?.message ?? "监控阈值触发"),
      time: String(alert.as_of ?? alert.triggered_at ?? ""),
      nav: "risk",
    });
  }
  for (const run of Array.isArray(workflows?.runs) ? workflows.runs : []) {
    items.push({
      key: `run-${run.workflow_run_id ?? Math.random()}`,
      icon: <Brain size={15} />,
      tone: run.status === "failed" ? "red" : run.status === "running" ? "blue" : "green",
      title: `${String(run.workflow_type ?? "Workflow")}${run.status === "failed" ? " · 失败" : run.status === "running" ? " · 运行中" : ""}`,
      detail: "金融团队 Workflow 审计运行",
      time: String(run.started_at ?? ""),
      nav: "agent",
    });
  }
  for (const decision of Array.isArray(memories?.decisions) ? memories.decisions : []) {
    items.push({
      key: `decision-${decision.decision_id ?? Math.random()}`,
      icon: <ClipboardCheck size={15} />,
      tone: "amber",
      title: `${String(decision.suggested_action ?? decision.decision_type ?? "决策记录")}${decision.asset_id ? ` · ${decision.asset_id}` : ""}`,
      detail: String(decision.summary ?? "决策已写入 Finance Memory"),
      time: String(decision.created_at ?? ""),
      nav: "memory",
    });
  }
  for (const warning of Array.isArray(portfolio?.concentration_warnings) ? portfolio.concentration_warnings : []) {
    items.push({
      key: `conc-${warning.symbol ?? Math.random()}`,
      icon: <Wallet size={15} />,
      tone: "amber",
      title: `集中度提示${warning.symbol ? ` · ${warning.symbol}` : ""}`,
      detail: String(warning.message ?? "持仓集中度超过阈值"),
      time: String(warning.as_of ?? ""),
      nav: "portfolio",
    });
  }

  items.sort((a, b) => b.time.localeCompare(a.time));
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const todayItems = items.filter((item) => item.time && new Date(item.time) >= todayStart).slice(0, 12);
  const earlierItems = items.filter((item) => item.time && new Date(item.time) < todayStart).slice(0, 12);
  const untimedItems = items.filter((item) => !item.time).slice(0, 6);

  return (
    <Panel title="今日动态" subtitle="风险触发、Workflow 审计、决策与复盘按时间聚合" icon={<CircleDot size={16} />}>
      <div className="feed-panel">
        {renderFeedGroup("今天", todayItems, onNavigate)}
        {renderFeedGroup("更早", earlierItems, onNavigate)}
        {renderFeedGroup("最近", untimedItems, onNavigate)}
        {!items.length ? (
          <div className="feed-empty">暂无动态。触发事件、Workflow 运行与决策记录会按时间出现在这里。</div>
        ) : null}
      </div>
    </Panel>
  );
}

function renderFeedGroup(label: string, items: FeedItem[], onNavigate?: (nav: NavId) => void) {
  if (!items.length) {
    return null;
  }
  return (
    <div className="feed-group">
      <span className="feed-group-label">{label}</span>
      <div className="feed-list">
        {items.map((item) => (
          <button key={item.key} type="button" className={`feed-item tone-${item.tone}`} onClick={() => onNavigate?.(item.nav)}>
            <span className="feed-item-icon">{item.icon}</span>
            <span className="feed-item-body">
              <span className="feed-item-title">
                <strong>{item.title}</strong>
              </span>
              <span className="feed-item-detail">{item.detail}</span>
            </span>
            <time className="feed-item-time">{formatFeedTime(item.time)}</time>
          </button>
        ))}
      </div>
    </div>
  );
}

function severityTone(severity: string): string {
  if (severity === "critical" || severity === "high") {
    return "red";
  }
  if (severity === "medium") {
    return "amber";
  }
  if (severity === "low") {
    return "blue";
  }
  return "green";
}

function formatFeedTime(value: string): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const time = date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  if (day === today) {
    return time;
  }
  if (day === today - 86400000) {
    return `昨天 ${time}`;
  }
  return `${date.getMonth() + 1}-${date.getDate()} ${time}`;
}

/* ---------------- 右侧快照栏 ---------------- */

function PortfolioSnapshotPanel({
  portfolio,
  onNavigate,
}: Pick<ConsolePageProps, "portfolio"> & OverviewExtraProps) {
  const positions = (Array.isArray(portfolio?.positions) ? portfolio.positions : []).slice(0, 6);
  return (
    <section className="panel snapshot-panel">
      <header className="panel-head">
        <div className="panel-icon">
          <Wallet size={16} />
        </div>
        <div>
          <h2>持仓快照</h2>
          <p>{portfolio?.metrics?.position_count ?? 0} 笔活跃持仓</p>
        </div>
      </header>
      {positions.length ? (
        <div className="position-mini-list">
          {positions.map((item: any, index: number) => {
            const pnl = Number(item.unrealized_pnl ?? 0);
            return (
              <div className="position-mini" key={`${item.symbol ?? index}-${item.side ?? ""}`}>
                <strong>{item.symbol}</strong>
                <span>{item.portfolio_weight ?? item.market ?? "-"}</span>
                <span className={pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : ""}>
                  {pnl !== 0 ? `${pnl > 0 ? "+" : ""}${pnl}` : "-"}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="snapshot-empty">暂无持仓数据，配置组合后展示快照。</div>
      )}
      <button type="button" className="snapshot-more" onClick={() => onNavigate?.("portfolio")}>
        查看持仓监控 →
      </button>
    </section>
  );
}

function AgentMiniPanel({
  workflows,
  models,
  modelPreview,
  onNavigate,
}: Pick<ConsolePageProps, "workflows" | "models" | "modelPreview"> & OverviewExtraProps) {
  const previewRoutes = Array.isArray(modelPreview?.data?.routes) ? modelPreview.data.routes : [];
  const agentModelNodes = buildAgentModelNodes(models, previewRoutes);
  return (
    <section className="panel snapshot-panel">
      <header className="panel-head">
        <div className="panel-icon">
          <Brain size={16} />
        </div>
        <div>
          <h2>Agent 状态</h2>
          <p>Workflow 与模型路由</p>
        </div>
      </header>
      <div className="position-mini-list">
        <div className="position-mini">
          <strong>可用 Workflow</strong>
          <span />
          <span>{workflows?.available?.length ?? 0} 个</span>
        </div>
        <div className="position-mini">
          <strong>运行中</strong>
          <span />
          <span>{workflows?.metrics?.running_count ?? 0}</span>
        </div>
        <div className="position-mini">
          <strong>失败运行</strong>
          <span />
          <span className={(workflows?.metrics?.failed_count ?? 0) > 0 ? "pnl-neg" : ""}>
            {workflows?.metrics?.failed_count ?? 0}
          </span>
        </div>
        <div className="position-mini">
          <strong>模型路由</strong>
          <span />
          <span>{agentModelNodes.length} 节点</span>
        </div>
      </div>
      <button type="button" className="snapshot-more" onClick={() => onNavigate?.("agent")}>
        查看 Agent 运行 →
      </button>
    </section>
  );
}
export function PendingActionsPanel({
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
export function PortfolioPanel({ portfolio }: Pick<ConsolePageProps, "portfolio">) {
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
export function RecommendationPanel({
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
export function AgentSummaryPanel({
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
export function RiskPanel({ risks }: Pick<ConsolePageProps, "risks">) {
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
export function MemoryPanel({ memories }: Pick<ConsolePageProps, "memories">) {
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
export function WatchlistPanel({ watchlists }: Pick<ConsolePageProps, "watchlists">) {
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
export function DataHealthPanel({ dataHealth }: Pick<ConsolePageProps, "dataHealth">) {
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
