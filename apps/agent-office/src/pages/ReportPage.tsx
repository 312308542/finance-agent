import React from "react";
import { FileText, RefreshCcw, ShieldAlert } from "lucide-react";
import { loadReportDetail, loadReports } from "../api";
import {
  buildReportPageModel,
  formatReviewStatus,
  type ReportListItem,
  type ReportPageModel,
  type ReportTone,
} from "../reportView";

type ReportPageProps = {
  ownerId: string;
};

const emptyListPayload = { status: "empty", items: [], metrics: {} };

export function ReportPage({ ownerId }: ReportPageProps) {
  const [listPayload, setListPayload] = React.useState<Record<string, any> | null>(null);
  const [detailPayload, setDetailPayload] = React.useState<Record<string, any> | null>(null);
  const [selectedRunId, setSelectedRunId] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const selectedRunIdRef = React.useRef<string | null>(null);

  const model = React.useMemo(
    () => buildReportPageModel(listPayload ?? emptyListPayload, detailPayload, selectedRunId),
    [detailPayload, listPayload, selectedRunId],
  );

  const refreshList = React.useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    setError(null);
    const payload = await loadReports(ownerId, 40);
    setListPayload(payload);
    const currentSelectedRunId = selectedRunIdRef.current;
    const nextModel = buildReportPageModel(payload, null, currentSelectedRunId);
    if (!currentSelectedRunId && nextModel.selected) {
      setSelectedRunId(nextModel.selected.workflowRunId);
      selectedRunIdRef.current = nextModel.selected.workflowRunId;
    }
    if (payload.status === "unavailable") {
      setError(String(payload.message ?? "报告列表暂不可用"));
    }
    setLoading(false);
  }, [ownerId]);

  React.useEffect(() => {
    void refreshList();
    const timer = window.setInterval(() => {
      void refreshList(true);
    }, 60000);
    return () => window.clearInterval(timer);
  }, [refreshList]);

  React.useEffect(() => {
    const runId = selectedRunId || model.selected?.workflowRunId || null;
    if (!runId) {
      setDetailPayload(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    loadReportDetail(runId)
      .then((payload) => {
        if (!cancelled) {
          setDetailPayload(payload);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setDetailLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [model.selected?.workflowRunId, selectedRunId]);

  return (
    <section className="report-workspace">
      <ReportListPanel
        model={model}
        loading={loading}
        error={error}
        selectedRunId={model.selected?.workflowRunId ?? null}
        onSelect={(workflowRunId) => {
          selectedRunIdRef.current = workflowRunId;
          setSelectedRunId(workflowRunId);
        }}
        onRefresh={() => void refreshList()}
      />
      <ReportDetailPanel model={model} loading={detailLoading} />
    </section>
  );
}

function ReportListPanel({
  model,
  loading,
  error,
  selectedRunId,
  onSelect,
  onRefresh,
}: {
  model: ReportPageModel;
  loading: boolean;
  error: string | null;
  selectedRunId: string | null;
  onSelect: (workflowRunId: string) => void;
  onRefresh: () => void;
}) {
  return (
    <aside className="report-list-panel panel">
      <div className="panel-head report-panel-head">
        <span className="panel-icon">
          <FileText size={16} />
        </span>
        <div>
          <h2>中文报告</h2>
          <p>按 Workflow 时间倒序展示，报告详情来自后端只读接口。</p>
        </div>
        <button className="icon-button" type="button" onClick={onRefresh} title="刷新报告">
          <RefreshCcw size={15} />
        </button>
      </div>

      <div className="report-metrics">
        <ReportMetric label="报告总数" value={model.metrics.totalCount} />
        <ReportMetric label="已复核" value={model.metrics.reviewedCount} />
        <ReportMetric label="待复核" value={model.metrics.pendingReviewCount} />
        <ReportMetric label="被驳回" value={model.metrics.rejectedCount} />
      </div>

      {error ? <div className="notice report-notice">{error}</div> : null}
      {loading && !model.items.length ? <div className="empty-state">正在加载报告列表...</div> : null}
      {!loading && !model.items.length ? <div className="empty-state">{model.emptyText}</div> : null}

      <div className="report-list">
        {model.items.map((item) => (
          <button
            key={item.workflowRunId}
            className={item.workflowRunId === selectedRunId ? "report-list-item is-active" : "report-list-item"}
            type="button"
            onClick={() => onSelect(item.workflowRunId)}
          >
            <span className={`report-dot tone-${item.reviewTone}`} />
            <span className="report-list-content">
              <span className="report-list-topline">
                <strong>{item.title}</strong>
                <em>{formatShortDate(item.createdAt)}</em>
              </span>
              <span className="report-list-summary">{item.summary || "暂无摘要，打开详情查看报告正文。"}</span>
              <span className="report-list-meta">
                <ReportPill tone={item.statusTone}>{item.statusLabel}</ReportPill>
                <ReportPill tone={item.reviewTone}>{item.reviewLabel}</ReportPill>
                <small>{item.workflowLabel}</small>
              </span>
            </span>
          </button>
        ))}
      </div>
    </aside>
  );
}

function ReportDetailPanel({ model, loading }: { model: ReportPageModel; loading: boolean }) {
  const selected = model.selected;
  const detail = model.detail;
  return (
    <section className="report-detail-panel panel">
      <div className="report-detail-header">
        <div>
          <span className="eyebrow">Report Detail</span>
          <h2>{selected?.title ?? "暂无中文报告"}</h2>
          <p>{selected?.summary || "选择左侧报告后查看结论、证据、风险反驳和确认边界。"}</p>
        </div>
        <div className="report-detail-status">
          {selected ? <ReportPill tone={selected.statusTone}>{selected.statusLabel}</ReportPill> : null}
          {selected ? <ReportPill tone={selected.reviewTone}>{selected.reviewLabel}</ReportPill> : null}
        </div>
      </div>

      {selected ? (
        <div className="report-detail-facts">
          <ReportFact label="Workflow" value={selected.workflowRunId} />
          <ReportFact label="类型" value={selected.workflowLabel} />
          <ReportFact label="推荐数" value={selected.recommendationCount || "—"} />
          <ReportFact label="风险数" value={selected.riskCount || "—"} />
        </div>
      ) : null}

      <nav className="report-anchor-nav" aria-label="报告章节导航">
        {(detail?.anchors ?? []).map((anchor) => (
          <a key={anchor.id} href={`#${anchor.id}`}>
            {anchor.label}
          </a>
        ))}
      </nav>

      <section className="report-markdown-shell">
        {loading ? <div className="empty-state">正在加载报告正文...</div> : null}
        {!loading && !selected ? <div className="empty-state">{model.emptyText}</div> : null}
        {!loading && selected && !detail?.markdown ? (
          <div className="empty-state">该 Workflow 暂未生成中文报告正文。</div>
        ) : null}
        {!loading && detail?.markdown ? (
          <article
            className="report-markdown"
            dangerouslySetInnerHTML={{ __html: detail.html }}
          />
        ) : null}
      </section>

      {detail?.reviewResults.length ? (
        <section className="report-review-box">
          <div className="report-review-title">
            <ShieldAlert size={15} />
            <strong>异步复核记录</strong>
            <span>{detail.reviewResults.length} 条</span>
          </div>
          <div className="report-review-list">
            {detail.reviewResults.map((item, index) => (
              <article key={`${item.review_status ?? item.status ?? "review"}-${index}`}>
                <strong>{formatReviewStatus(String(item.review_status ?? item.status ?? "")).label}</strong>
                <p>{String(item.summary ?? item.reasons?.[0] ?? item.verdict ?? "暂无复核摘要")}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}
    </section>
  );
}

function ReportMetric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <article className="report-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function ReportFact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <article>
      <span>{label}</span>
      <strong title={String(value)}>{value}</strong>
    </article>
  );
}

function ReportPill({ tone, children }: { tone: ReportTone; children: React.ReactNode }) {
  return <span className={`report-pill tone-${tone}`}>{children}</span>;
}

function formatShortDate(value: string): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
