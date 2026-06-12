import { AlertTriangle, BarChart3, Brain, CheckCircle2, CircleDot, Clock3, Database, GitBranch, ListChecks, ServerCog, ShieldAlert, Wallet } from "lucide-react";
import { buildAgentModelNodes } from "../agentModelRoutes";
import type { ConsolePageProps } from "../consoleTypes";
import { ActionRow, DataTable, MetricBlock, Panel, Timeline } from "../components/consoleCommon";
import { DataSyncControlPanel } from "./DataSyncControlPanel";

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
