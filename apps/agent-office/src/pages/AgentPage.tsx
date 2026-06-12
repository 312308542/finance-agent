import * as React from "react";
import { Brain, CheckCircle2, Clock3, Workflow } from "lucide-react";
import { loadWorkflowOverview } from "../api";
import { buildAgentPageModel } from "../consolePagesView";

type AgentPageProps = {
  ownerId: string;
  initialPayload?: Record<string, any> | null;
};

export function AgentPage({ ownerId, initialPayload = null }: AgentPageProps) {
  const [payload, setPayload] = React.useState<Record<string, any> | null>(initialPayload);
  const [loading, setLoading] = React.useState(!initialPayload);
  const [error, setError] = React.useState<string | null>(null);
  const model = React.useMemo(() => buildAgentPageModel(payload), [payload]);

  const refresh = React.useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    setError(null);
    try {
      setPayload(await loadWorkflowOverview(ownerId, 80));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [ownerId]);

  React.useEffect(() => {
    void refresh(Boolean(initialPayload));
    const timer = window.setInterval(() => void refresh(true), 60000);
    return () => window.clearInterval(timer);
  }, [refresh, initialPayload]);

  return (
    <section className="real-page-grid">
      <header className="real-page-head">
        <div>
          <p className="eyebrow">Agent Runtime</p>
          <h2>Agent 运行</h2>
        </div>
        <button className="button" onClick={() => void refresh()} disabled={loading}>
          刷新
        </button>
      </header>
      {error ? <div className="notice notice-red">{error}</div> : null}
      <div className="real-metric-grid">
        <Metric label="可用 Workflow" value={model.metrics.availableCount} />
        <Metric label="最近运行" value={model.metrics.recentCount} />
        <Metric label="运行中" value={model.metrics.runningCount} />
        <Metric label="失败" value={model.metrics.failedCount} />
      </div>
      <section className="real-page-columns">
        <article className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Workflow Catalog</p>
              <h2>可用流程</h2>
            </div>
            <Workflow size={16} />
          </div>
          <div className="workflow-chip-list">
            {model.available.length ? model.available.map((item: any) => (
              <span key={item.workflow_type || item.description}>
                {item.workflow_type || item.description}
              </span>
            )) : <p className="empty-copy">暂无可用 Workflow</p>}
          </div>
        </article>
        <article className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Runtime</p>
              <h2>运行状态</h2>
            </div>
            <Brain size={16} />
          </div>
          <div className="agent-runtime-stack">
            <RuntimeBadge icon={<CheckCircle2 size={15} />} label="成功/最近" value={model.metrics.recentCount} />
            <RuntimeBadge icon={<Clock3 size={15} />} label="运行中" value={model.metrics.runningCount} />
          </div>
        </article>
      </section>
      <article className="panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Audit Trail</p>
            <h2>Workflow 审计</h2>
          </div>
        </div>
        <div className="real-table real-table-workflows">
          {model.runs.length ? model.runs.map((item) => (
            <div className="real-table-row" key={item.workflowRunId}>
              <strong>{item.workflowType}</strong>
              <span>{item.status}</span>
              <span>{item.durationDisplay}</span>
              <span>{item.modelSourceDisplay}</span>
              <span>{item.reviewStatus || "-"}</span>
              <span>{item.startedAt || "-"}</span>
            </div>
          )) : <p className="empty-copy">{model.emptyText}</p>}
        </div>
      </article>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <article className="real-metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function RuntimeBadge({ icon, label, value }: { icon: React.ReactNode; label: string; value: React.ReactNode }) {
  return (
    <div className="runtime-badge">
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
