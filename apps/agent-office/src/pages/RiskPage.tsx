import * as React from "react";
import { AlertTriangle, Bell, ShieldAlert } from "lucide-react";
import { loadRiskOverview } from "../api";
import { buildRiskPageModel } from "../consolePagesView";

type RiskPageProps = {
  ownerId: string;
  initialPayload?: Record<string, any> | null;
};

export function RiskPage({ ownerId, initialPayload = null }: RiskPageProps) {
  const [payload, setPayload] = React.useState<Record<string, any> | null>(initialPayload);
  const [loading, setLoading] = React.useState(!initialPayload);
  const [error, setError] = React.useState<string | null>(null);
  const model = React.useMemo(() => buildRiskPageModel(payload), [payload]);

  const refresh = React.useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    setError(null);
    try {
      setPayload(await loadRiskOverview(ownerId, 80));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [ownerId]);

  React.useEffect(() => {
    void refresh(Boolean(initialPayload));
    const timer = window.setInterval(() => void refresh(true), 30000);
    return () => window.clearInterval(timer);
  }, [refresh, initialPayload]);

  return (
    <section className="real-page-grid">
      <header className="real-page-head">
        <div>
          <p className="eyebrow">Risk Center</p>
          <h2>风险中心</h2>
        </div>
        <button className="button" onClick={() => void refresh()} disabled={loading}>
          刷新
        </button>
      </header>
      {error ? <div className="notice notice-red">{error}</div> : null}
      <div className="real-metric-grid">
        <Metric label="未处理提醒" value={model.metrics.alertCount} />
        <Metric label="触发事件" value={model.metrics.triggerCount} />
        <Metric label="风险发现" value={model.metrics.riskFindingCount} />
        <Metric label="高优先级" value={model.metrics.highSeverityCount} />
      </div>
      <section className="severity-strip">
        {model.severityBreakdown.map((item) => (
          <div className={`severity-item tone-${item.tone}`} key={item.label}>
            <span>{item.label}</span>
            <strong>{item.display}</strong>
          </div>
        ))}
      </section>
      <section className="real-page-columns">
        <article className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Alerts</p>
              <h2>提醒与触发</h2>
            </div>
            <Bell size={16} />
          </div>
          <div className="event-stack">
            {model.events.length ? model.events.map((item) => (
              <div className={`event-row tone-${item.tone}`} key={`${item.itemType}:${item.id}`}>
                <ShieldAlert size={15} />
                <div>
                  <strong>{item.title}</strong>
                  <span>{item.severityLabel} · {item.status || "-"} · {item.timestamp || "-"}</span>
                  <p>{item.detail || "-"}</p>
                </div>
              </div>
            )) : <p className="empty-copy">{model.emptyText}</p>}
          </div>
        </article>
        <article className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Findings</p>
              <h2>风险发现</h2>
            </div>
            <AlertTriangle size={16} />
          </div>
          <div className="event-stack">
            {model.findings.length ? model.findings.map((item) => (
              <div className={`event-row tone-${item.tone}`} key={item.riskId}>
                <AlertTriangle size={15} />
                <div>
                  <strong>{item.title}</strong>
                  <span>{item.severityLabel} · {item.riskType} · {item.asOf || "-"}</span>
                  <p>{item.description || item.scoreDisplay}</p>
                </div>
              </div>
            )) : <p className="empty-copy">暂无风险发现</p>}
          </div>
        </article>
      </section>
      <article className="panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Data Quality</p>
            <h2>数据质量异常</h2>
          </div>
        </div>
        <div className="real-table">
          {model.dataQuality.length ? model.dataQuality.map((item) => (
            <div className="real-table-row" key={item.qualityId || item.title}>
              <strong>{item.title}</strong>
              <span>{item.provider || "-"}</span>
              <span>{item.status || "-"}</span>
              <span>问题 {item.issueCount}</span>
              <span>{item.latestDataAt || "-"}</span>
            </div>
          )) : <p className="empty-copy">暂无数据质量异常</p>}
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
