import * as React from "react";
import { AlertTriangle, BriefcaseBusiness, Wallet } from "lucide-react";
import { loadPortfolioOverview } from "../api";
import { buildPortfolioPageModel } from "../consolePagesView";

type PortfolioPageProps = {
  ownerId: string;
  initialPayload?: Record<string, any> | null;
};

export function PortfolioPage({ ownerId, initialPayload = null }: PortfolioPageProps) {
  const [payload, setPayload] = React.useState<Record<string, any> | null>(initialPayload);
  const [loading, setLoading] = React.useState(!initialPayload);
  const [error, setError] = React.useState<string | null>(null);
  const model = React.useMemo(() => buildPortfolioPageModel(payload), [payload]);

  const refresh = React.useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    setError(null);
    try {
      setPayload(await loadPortfolioOverview(ownerId));
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
          <p className="eyebrow">Portfolio Monitor</p>
          <h2>持仓监控</h2>
        </div>
        <button className="button" onClick={() => void refresh()} disabled={loading}>
          刷新
        </button>
      </header>
      {error ? <div className="notice notice-red">{error}</div> : null}
      <div className="real-metric-grid">
        <Metric label="活跃持仓" value={model.metrics.positionCount} />
        <Metric label="盈利持仓" value={model.metrics.positivePositionCount} />
        <Metric label="亏损持仓" value={model.metrics.negativePositionCount} />
        <Metric label="最大持仓权重" value={model.metrics.maxPositionWeightDisplay} />
      </div>
      {model.concentrationWarnings.length ? (
        <section className="real-alert-band">
          <AlertTriangle size={16} />
          <div>
            <strong>集中度提示</strong>
            <span>{model.concentrationWarnings[0].message}</span>
          </div>
        </section>
      ) : null}
      <section className="real-page-columns">
        <article className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Positions</p>
              <h2>持仓明细</h2>
            </div>
            <Wallet size={16} />
          </div>
          <div className="real-table">
            {model.positions.length ? model.positions.map((item) => (
              <div className="real-table-row" key={`${item.assetId}:${item.side}`}>
                <strong>{item.assetLabel}</strong>
                <span>{item.market}</span>
                <span>{item.side}</span>
                <span>{item.marketValue || "-"}</span>
                <span>{item.unrealizedPnl || "-"}</span>
                <span>{item.weightDisplay}</span>
              </div>
            )) : <p className="empty-copy">{model.emptyText}</p>}
          </div>
        </article>
        <article className="panel">
          <div className="panel-head">
            <div>
              <p className="eyebrow">Concentration</p>
              <h2>集中度</h2>
            </div>
            <BriefcaseBusiness size={16} />
          </div>
          <WeightList title="市场权重" items={model.marketWeights} />
          <WeightList title="行业权重" items={model.industryWeights} />
        </article>
      </section>
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

function WeightList({ title, items }: { title: string; items: Array<{ label: string; display: string }> }) {
  return (
    <div className="weight-list">
      <h3>{title}</h3>
      {items.length ? items.map((item) => (
        <div className="weight-list-row" key={item.label}>
          <span>{item.label}</span>
          <strong>{item.display}</strong>
        </div>
      )) : <p className="empty-copy">暂无权重数据</p>}
    </div>
  );
}
