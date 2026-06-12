import * as React from "react";
import { GitBranch, History, Sparkles } from "lucide-react";
import { loadRecentMemories } from "../api";
import { buildMemoryPageModel } from "../consolePagesView";

type MemoryPageProps = {
  ownerId: string;
  initialPayload?: Record<string, any> | null;
};

export function MemoryPage({ ownerId, initialPayload = null }: MemoryPageProps) {
  const [payload, setPayload] = React.useState<Record<string, any> | null>(initialPayload);
  const [loading, setLoading] = React.useState(!initialPayload);
  const [error, setError] = React.useState<string | null>(null);
  const model = React.useMemo(() => buildMemoryPageModel(payload), [payload]);

  const refresh = React.useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    setError(null);
    try {
      setPayload(await loadRecentMemories(ownerId, 80));
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
          <p className="eyebrow">Finance Memory</p>
          <h2>Finance Memory</h2>
        </div>
        <button className="button" onClick={() => void refresh()} disabled={loading}>
          刷新
        </button>
      </header>
      {error ? <div className="notice notice-red">{error}</div> : null}
      <div className="real-metric-grid">
        <Metric label="记忆条数" value={model.metrics.memoryCount} />
        <Metric label="过期记忆" value={model.metrics.staleMemoryCount} />
        <Metric label="覆盖资产" value={model.metrics.assetCount} />
        <Metric label="状态" value={model.status} />
      </div>
      <section className="memory-timeline panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Timeline</p>
            <h2>记忆时间线</h2>
          </div>
          <History size={16} />
        </div>
        <div className="memory-stack">
          {model.items.length ? model.items.map((item) => (
            <article className="memory-item" key={item.memoryId}>
              <div className="memory-icon">
                <Sparkles size={15} />
              </div>
              <div>
                <div className="memory-item-head">
                  <strong>{item.typeLabel}</strong>
                  <span>{item.confidenceDisplay}</span>
                </div>
                <p>{item.content}</p>
                <em>{item.assetId || item.scope || "全局"} · {item.status || "-"} · {item.createdAt || "-"}</em>
              </div>
            </article>
          )) : <p className="empty-copy">{model.emptyText}</p>}
        </div>
      </section>
      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Memory Boundary</p>
            <h2>写入来源</h2>
          </div>
          <GitBranch size={16} />
        </div>
        <p className="empty-copy">
          页面只展示已入库的用户反馈、复盘结论和长期偏好，不在前端生成或改写记忆。
        </p>
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
