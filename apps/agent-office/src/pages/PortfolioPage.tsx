import * as React from "react";
import { AlertTriangle, BriefcaseBusiness, ClipboardList, Wallet } from "lucide-react";
import {
  loadExecutionRecords,
  loadOrderDrafts,
  loadPortfolioOverview,
  recordExecution,
} from "../api";
import {
  buildExecutionPayload,
  buildExecutionsModel,
  buildOrderDraftsModel,
  type ExecutionFormValues,
  type OrderDraftModel,
} from "../actionLoopView";
import { buildPortfolioPageModel } from "../consolePagesView";

type PortfolioPageProps = {
  ownerId: string;
  initialPayload?: Record<string, any> | null;
};

export function PortfolioPage({ ownerId, initialPayload = null }: PortfolioPageProps) {
  const [payload, setPayload] = React.useState<Record<string, any> | null>(initialPayload);
  const [draftPayload, setDraftPayload] = React.useState<Record<string, any> | null>(null);
  const [executionPayload, setExecutionPayload] = React.useState<Record<string, any> | null>(null);
  const [loading, setLoading] = React.useState(!initialPayload);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [savingExecution, setSavingExecution] = React.useState(false);
  const [formValues, setFormValues] = React.useState({
    portfolioId: "",
    assetId: "",
    market: "ashare",
    action: "buy",
    executedPrice: "",
    executedQuantity: "",
    executedAt: "",
    orderDraftId: "",
    decisionLogId: "",
    fee: "",
    note: "",
  });
  const model = React.useMemo(() => buildPortfolioPageModel(payload), [payload]);
  const draftModel = React.useMemo(() => buildOrderDraftsModel(draftPayload), [draftPayload]);
  const executionModel = React.useMemo(() => buildExecutionsModel(executionPayload), [executionPayload]);

  const refresh = React.useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    setError(null);
    try {
      const [portfolioData, drafts, executions] = await Promise.all([
        loadPortfolioOverview(ownerId),
        loadOrderDrafts(ownerId, "drafted", 80),
        loadExecutionRecords(ownerId, null, 80),
      ]);
      setPayload(portfolioData);
      setDraftPayload(drafts);
      setExecutionPayload(executions);
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

  React.useEffect(() => {
    const portfolioId = model.activePortfolioId || String(model.portfolios[0]?.portfolio_id ?? "");
    if (portfolioId && !formValues.portfolioId) {
      setFormValues((values) => ({ ...values, portfolioId }));
    }
  }, [formValues.portfolioId, model.activePortfolioId, model.portfolios]);

  const selectDraft = (draftId: string) => {
    const draft = draftModel.items.find((item) => item.orderDraftId === draftId);
    setFormValues((values) => ({
      ...values,
      orderDraftId: draftId,
      decisionLogId: draft?.decisionLogId ?? "",
      portfolioId: draft?.portfolioId || values.portfolioId,
      assetId: draft?.assetId || values.assetId,
      market: draft?.market || values.market,
      action: draft?.action || values.action,
    }));
  };

  const submitExecution = async () => {
    setSavingExecution(true);
    setNotice(null);
    const executionValues: ExecutionFormValues = {
        ownerId,
        portfolioId: formValues.portfolioId,
        assetId: formValues.assetId,
        market: formValues.market,
        action: formValues.action,
        executedPrice: formValues.executedPrice,
        executedQuantity: formValues.executedQuantity,
        executedAt: formValues.executedAt,
        orderDraftId: formValues.orderDraftId || null,
        decisionLogId: formValues.decisionLogId || null,
        fee: formValues.fee || null,
        note: formValues.note || null,
    };
    const result = await recordExecution(buildExecutionPayload(executionValues));
    if (result.status === "ok") {
      setNotice("执行登记已保存，持仓与后续复盘提醒会同步更新。");
      setExecutionPayload(await loadExecutionRecords(ownerId, null, 80));
      setPayload(await loadPortfolioOverview(ownerId));
    } else {
      setNotice(String(result.message ?? "执行登记失败"));
    }
    setSavingExecution(false);
  };

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
      {notice ? <div className="notice recommendation-notice">{notice}</div> : null}
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
      <section className="real-page-columns action-loop-columns" id="execution-registration">
        <ExecutionRegistrationPanel
          drafts={draftModel.items}
          values={formValues}
          saving={savingExecution}
          onChange={(key, value) => setFormValues((current) => ({ ...current, [key]: value }))}
          onSelectDraft={selectDraft}
          onSubmit={() => void submitExecution()}
        />
        <ExecutionHistoryPanel executions={executionModel.items} emptyText={executionModel.emptyText} />
      </section>
    </section>
  );
}

function ExecutionRegistrationPanel({
  drafts,
  values,
  saving,
  onChange,
  onSelectDraft,
  onSubmit,
}: {
  drafts: OrderDraftModel[];
  values: {
    portfolioId: string;
    assetId: string;
    market: string;
    action: string;
    executedPrice: string;
    executedQuantity: string;
    executedAt: string;
    orderDraftId: string;
    decisionLogId: string;
    fee: string;
    note: string;
  };
  saving: boolean;
  onChange: (key: keyof typeof values, value: string) => void;
  onSelectDraft: (draftId: string) => void;
  onSubmit: () => void;
}) {
  const disabled =
    saving || !values.portfolioId || !values.assetId || !values.executedPrice || !values.executedQuantity || !values.executedAt;
  return (
    <article className="panel action-loop-panel">
      <div className="panel-head">
        <div>
          <p className="eyebrow">Execution Registration</p>
          <h2>执行登记</h2>
          <p>仅记录用户已在外部完成的结果，用于持仓记账和后续复盘。</p>
        </div>
        <ClipboardList size={16} />
      </div>
      <div className="action-loop-form">
        <label>
          <span>来源草案</span>
          <select value={values.orderDraftId} onChange={(event) => onSelectDraft(event.target.value)}>
            <option value="">自主登记或暂不关联</option>
            {drafts.map((draft) => (
              <option key={draft.orderDraftId} value={draft.orderDraftId}>
                {draft.assetLabel} · {draft.actionLabel} · {draft.priceRangeDisplay}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>组合 ID</span>
          <input value={values.portfolioId} onChange={(event) => onChange("portfolioId", event.target.value)} />
        </label>
        <label>
          <span>资产 ID</span>
          <input placeholder="ashare:600519" value={values.assetId} onChange={(event) => onChange("assetId", event.target.value)} />
        </label>
        <label>
          <span>市场</span>
          <input value={values.market} onChange={(event) => onChange("market", event.target.value)} />
        </label>
        <label>
          <span>动作</span>
          <select value={values.action} onChange={(event) => onChange("action", event.target.value)}>
            <option value="buy">买入</option>
            <option value="add">加仓</option>
            <option value="sell">卖出</option>
            <option value="reduce">减仓</option>
          </select>
        </label>
        <label>
          <span>执行价格</span>
          <input value={values.executedPrice} onChange={(event) => onChange("executedPrice", event.target.value)} />
        </label>
        <label>
          <span>执行数量</span>
          <input value={values.executedQuantity} onChange={(event) => onChange("executedQuantity", event.target.value)} />
        </label>
        <label>
          <span>执行时间</span>
          <input placeholder="2026-06-13T10:05:00+08:00" value={values.executedAt} onChange={(event) => onChange("executedAt", event.target.value)} />
        </label>
        <label>
          <span>费用</span>
          <input value={values.fee} onChange={(event) => onChange("fee", event.target.value)} />
        </label>
        <label className="action-loop-form-wide">
          <span>备注</span>
          <textarea value={values.note} onChange={(event) => onChange("note", event.target.value)} />
        </label>
      </div>
      <button className="button button-primary" type="button" disabled={disabled} onClick={onSubmit}>
        {saving ? "保存中" : "保存执行登记"}
      </button>
    </article>
  );
}

function ExecutionHistoryPanel({
  executions,
  emptyText,
}: {
  executions: ReturnType<typeof buildExecutionsModel>["items"];
  emptyText: string;
}) {
  return (
    <article className="panel action-loop-panel">
      <div className="panel-head">
        <div>
          <p className="eyebrow">Execution History</p>
          <h2>执行历史</h2>
          <p>按最近登记时间展示，用于检查建议、草案和实际登记之间的闭环。</p>
        </div>
        <Wallet size={16} />
      </div>
      <div className="event-stack">
        {executions.length ? executions.map((item) => (
          <div className="event-row tone-blue" key={item.executionId}>
            <ClipboardList size={15} />
            <div>
              <strong>{item.assetLabel} · {item.actionLabel}</strong>
              <span>{item.priceDisplay} × {item.quantityDisplay} · {item.executedAt || "-"}</span>
              <p>{item.note || item.sourceLabel}</p>
            </div>
          </div>
        )) : <p className="empty-copy">{emptyText}</p>}
      </div>
    </article>
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
