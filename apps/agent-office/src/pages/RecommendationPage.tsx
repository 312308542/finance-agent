import React from "react";
import { BarChart3, ChevronRight, MessageSquareText, RefreshCcw, ShieldAlert } from "lucide-react";
import {
  loadLatestRecommendations,
  loadPendingDecisions,
  submitDecisionFeedback,
} from "../api";
import {
  buildDecisionFeedbackPayload,
  buildRecommendationPageModel,
  mergeRecommendationPayloads,
  type DecisionFeedbackType,
  type RecommendationItemModel,
  type RecommendationPageModel,
  type RecommendationTone,
} from "../recommendationView";

type RecommendationPageProps = {
  ownerId: string;
};

const emptyRecommendationPayload = { status: "empty", runs: [], recommendations: [], metrics: {} };
const emptyDecisionPayload = { status: "empty", data: { items: [] } };
const recommendationMarkets = ["ashare", "crypto_spot", "crypto_future"];

export function RecommendationPage({ ownerId }: RecommendationPageProps) {
  const [recommendationPayload, setRecommendationPayload] = React.useState<Record<string, any> | null>(null);
  const [decisionPayload, setDecisionPayload] = React.useState<Record<string, any> | null>(null);
  const [selectedMarket, setSelectedMarket] = React.useState("ashare");
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const [feedbackTarget, setFeedbackTarget] = React.useState<RecommendationItemModel | null>(null);
  const [feedbackType, setFeedbackType] = React.useState<DecisionFeedbackType>("accepted");
  const [feedbackComment, setFeedbackComment] = React.useState("");
  const [modifiedAction, setModifiedAction] = React.useState("watch_only");
  const [notice, setNotice] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [savingFeedback, setSavingFeedback] = React.useState(false);

  const model = React.useMemo(
    () =>
      buildRecommendationPageModel(
        recommendationPayload ?? emptyRecommendationPayload,
        decisionPayload ?? emptyDecisionPayload,
        selectedMarket,
      ),
    [decisionPayload, recommendationPayload, selectedMarket],
  );

  const refresh = React.useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    const [recommendationResults, decisions] = await Promise.all([
      Promise.all(recommendationMarkets.map((market) => loadLatestRecommendations(ownerId, market, 80))),
      loadPendingDecisions(ownerId, 80),
    ]);
    const recommendations = mergeRecommendationPayloads(recommendationResults);
    setRecommendationPayload(recommendations);
    setDecisionPayload(decisions);
    if (recommendations.status === "unavailable" || decisions.status === "unavailable") {
      setNotice(String(recommendations.message ?? decisions.message ?? "推荐数据暂不可用"));
    } else {
      setNotice(null);
    }
    setLoading(false);
  }, [ownerId]);

  React.useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void refresh(true);
    }, 60000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const submitFeedback = async () => {
    if (!feedbackTarget?.pendingDecision) {
      return;
    }
    setSavingFeedback(true);
    const result = await submitDecisionFeedback(
      feedbackTarget.pendingDecision.decisionId,
      buildDecisionFeedbackPayload(feedbackType, feedbackComment, modifiedAction),
    );
    if (result.status === "ok") {
      setNotice("反馈已记录，并将进入 Finance Memory 闭环。");
      setFeedbackTarget(null);
      setFeedbackComment("");
      setDecisionPayload(await loadPendingDecisions(ownerId, 80));
    } else {
      setNotice(String(result.message ?? "反馈提交失败"));
    }
    setSavingFeedback(false);
  };

  return (
    <section className="recommendation-workspace">
      <section className="recommendation-panel panel">
        <RecommendationHeader
          model={model}
          loading={loading}
          notice={notice}
          onRefresh={() => void refresh()}
        />
        <RecommendationMarketTabs
          model={model}
          selectedMarket={model.selectedMarket}
          onSelect={setSelectedMarket}
        />
        <AvoidPoolSummary model={model} />
        <RecommendationTable
          model={model}
          expandedId={expandedId}
          onToggle={(id) => setExpandedId(expandedId === id ? null : id)}
          onFeedback={setFeedbackTarget}
        />
      </section>

      {feedbackTarget ? (
        <FeedbackDialog
          item={feedbackTarget}
          feedbackType={feedbackType}
          setFeedbackType={setFeedbackType}
          comment={feedbackComment}
          setComment={setFeedbackComment}
          modifiedAction={modifiedAction}
          setModifiedAction={setModifiedAction}
          saving={savingFeedback}
          onClose={() => setFeedbackTarget(null)}
          onSubmit={submitFeedback}
        />
      ) : null}
    </section>
  );
}

function RecommendationHeader({
  model,
  loading,
  notice,
  onRefresh,
}: {
  model: RecommendationPageModel;
  loading: boolean;
  notice: string | null;
  onRefresh: () => void;
}) {
  return (
    <>
      <div className="recommendation-head">
        <div className="panel-head">
          <span className="panel-icon">
            <BarChart3 size={16} />
          </span>
          <div>
            <h2>推荐决策</h2>
            <p>按市场分组展示候选建议、风险反驳、评分拆解和待确认反馈入口。</p>
          </div>
        </div>
        <button className="button" type="button" onClick={onRefresh} disabled={loading}>
          <RefreshCcw size={15} />
          {loading ? "刷新中" : "刷新"}
        </button>
      </div>
      <div className="recommendation-metrics">
        <RecommendationMetric label="候选建议" value={model.metrics.recommendationCount} />
        <RecommendationMetric label="候选买入" value={model.metrics.buyCount} />
        <RecommendationMetric label="建议观察" value={model.metrics.watchCount} />
        <RecommendationMetric label="待确认" value={model.metrics.pendingDecisionCount} />
      </div>
      {notice ? <div className="notice recommendation-notice">{notice}</div> : null}
    </>
  );
}

function RecommendationMarketTabs({
  model,
  selectedMarket,
  onSelect,
}: {
  model: RecommendationPageModel;
  selectedMarket: string;
  onSelect: (market: string) => void;
}) {
  return (
    <div className="recommendation-tabs" role="tablist" aria-label="推荐市场">
      {model.marketTabs.map((tab) => (
        <button
          key={tab.id}
          className={tab.id === selectedMarket ? "is-active" : ""}
          type="button"
          role="tab"
          aria-selected={tab.id === selectedMarket}
          onClick={() => onSelect(tab.id)}
        >
          <span>{tab.label}</span>
          <em>{tab.count}</em>
        </button>
      ))}
    </div>
  );
}

function AvoidPoolSummary({ model }: { model: RecommendationPageModel }) {
  return (
    <section className="avoid-pool-summary">
      <div>
        <ShieldAlert size={16} />
        <strong>回避池剔除</strong>
        <span>{model.avoidPoolSummary.description}</span>
      </div>
      {model.avoidPoolSummary.assets.length ? (
        <details>
          <summary>查看明细</summary>
          <div className="avoid-pool-assets">
            {model.avoidPoolSummary.assets.slice(0, 8).map((asset, index) => (
              <span key={`${asset.asset_id ?? asset.symbol ?? index}`}>
                {String(asset.symbol ?? asset.asset_id ?? "未知标的")} · {String(asset.reason ?? "已剔除")}
              </span>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

function RecommendationTable({
  model,
  expandedId,
  onToggle,
  onFeedback,
}: {
  model: RecommendationPageModel;
  expandedId: string | null;
  onToggle: (recommendationId: string) => void;
  onFeedback: (item: RecommendationItemModel) => void;
}) {
  if (!model.items.length) {
    return <div className="empty-state">{model.emptyText}</div>;
  }
  return (
    <div className="recommendation-table">
      <div className="recommendation-table-head">
        <span>排名</span>
        <span>标的</span>
        <span>动作</span>
        <span>评分</span>
        <span>置信度</span>
        <span>风险</span>
        <span>操作</span>
      </div>
      {model.items.map((item) => (
        <article key={item.recommendationId || item.assetId} className="recommendation-row">
          <button className="recommendation-row-main" type="button" onClick={() => onToggle(item.recommendationId)}>
            <span className="rank-cell">{item.rank || "—"}</span>
            <span className="asset-cell">
              <strong>{item.assetLabel || item.assetId}</strong>
              <em>{item.marketLabel}</em>
            </span>
            <RecommendationPill tone={item.actionTone}>{item.actionLabel}</RecommendationPill>
            <strong className="numeric-cell">{item.scoreDisplay}</strong>
            <span className="numeric-cell">{item.confidenceDisplay}</span>
            <span className={item.riskCount > 0 ? "risk-cell has-risk" : "risk-cell"}>
              {item.riskCount}
            </span>
            <span className="recommendation-actions">
              <ChevronRight size={15} />
            </span>
          </button>
          {expandedId === item.recommendationId ? (
            <RecommendationExpanded item={item} onFeedback={onFeedback} />
          ) : null}
        </article>
      ))}
    </div>
  );
}

function RecommendationExpanded({
  item,
  onFeedback,
}: {
  item: RecommendationItemModel;
  onFeedback: (item: RecommendationItemModel) => void;
}) {
  return (
    <section className="recommendation-expanded">
      <div>
        <h3>评分拆解</h3>
        {item.scoreBreakdown.length ? (
          <div className="score-breakdown-list">
            {item.scoreBreakdown.map((score) => (
              <span key={score.group} className={`tone-${score.tone}`}>
                {score.group}: {score.value}
              </span>
            ))}
          </div>
        ) : (
          <p>暂无评分拆解。</p>
        )}
      </div>
      <div>
        <h3>风险反驳</h3>
        <p>{item.riskRebuttal || item.summary || "暂无风险反驳文本。"}</p>
      </div>
      <div className="recommendation-expanded-actions">
        {item.reportWorkflowRunId ? (
          <a className="button button-ghost" href={`#report:${encodeURIComponent(item.reportWorkflowRunId)}`}>
            查看报告
          </a>
        ) : (
          <span>暂无关联报告</span>
        )}
        <button
          className="button button-primary"
          type="button"
          disabled={!item.pendingDecision}
          onClick={() => onFeedback(item)}
        >
          <MessageSquareText size={15} />
          {item.pendingDecision ? "反馈确认" : "无待确认决策"}
        </button>
      </div>
    </section>
  );
}

function FeedbackDialog({
  item,
  feedbackType,
  setFeedbackType,
  comment,
  setComment,
  modifiedAction,
  setModifiedAction,
  saving,
  onClose,
  onSubmit,
}: {
  item: RecommendationItemModel;
  feedbackType: DecisionFeedbackType;
  setFeedbackType: (value: DecisionFeedbackType) => void;
  comment: string;
  setComment: (value: string) => void;
  modifiedAction: string;
  setModifiedAction: (value: string) => void;
  saving: boolean;
  onClose: () => void;
  onSubmit: () => void;
}) {
  return (
    <div className="feedback-dialog-backdrop" role="presentation">
      <section className="feedback-dialog" role="dialog" aria-modal="true" aria-label="推荐反馈">
        <header>
          <div>
            <span className="eyebrow">Decision Feedback</span>
            <h2>{item.assetLabel}</h2>
            <p>{item.pendingDecision?.summary || item.summary || "反馈会写入决策日志和 Finance Memory。"}</p>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭反馈">
            ×
          </button>
        </header>
        <div className="feedback-options">
          {[
            ["accepted", "采纳"],
            ["rejected", "拒绝"],
            ["modified", "修改"],
            ["deferred", "暂缓"],
          ].map(([value, label]) => (
            <button
              key={value}
              className={feedbackType === value ? "is-active" : ""}
              type="button"
              onClick={() => setFeedbackType(value as DecisionFeedbackType)}
            >
              {label}
            </button>
          ))}
        </div>
        {feedbackType === "modified" ? (
          <label className="feedback-field">
            <span>修改后的动作</span>
            <input value={modifiedAction} onChange={(event) => setModifiedAction(event.target.value)} />
          </label>
        ) : null}
        <label className="feedback-field">
          <span>备注</span>
          <textarea
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="记录你的确认理由、保留条件或暂缓原因。"
          />
        </label>
        <footer>
          <button className="button button-ghost" type="button" onClick={onClose}>
            取消
          </button>
          <button className="button button-primary" type="button" disabled={saving} onClick={onSubmit}>
            {saving ? "提交中" : "提交反馈"}
          </button>
        </footer>
      </section>
    </div>
  );
}

function RecommendationMetric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <article>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function RecommendationPill({ tone, children }: { tone: RecommendationTone; children: React.ReactNode }) {
  return <span className={`recommendation-pill tone-${tone}`}>{children}</span>;
}
