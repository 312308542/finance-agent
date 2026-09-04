import * as React from "react";
import { AlertTriangle, BriefcaseBusiness, ClipboardList, Wallet } from "lucide-react";
import { Button, Input, Select } from "antd";
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
import { buildPositionMonitorModel, monitoringActionLabel } from "../positionMonitorView";

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
  const monitorModel = React.useMemo(() => buildPositionMonitorModel(payload), [payload]);
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
          <p>持仓体检、集中度提示与执行登记闭环。</p>
        </div>
        <Button onClick={() => void refresh()} loading={loading}>
          刷新
        </Button>
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
      <section className="panel position-monitor-panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Intraday Monitoring</p>
            <h2>盘中持仓动作</h2>
            <p>建议动作与实际执行分离展示；T+1、停牌或行情过期时只标记暂不可执行。</p>
          </div>
          <AlertTriangle size={16} />
        </div>
        {monitorModel.items.length ? (
          <div className="position-monitor-list">
            {monitorModel.items.map((item) => (
              <article className={`position-monitor-card severity-${item.severity}`} key={item.positionId}>
                <header>
                  <strong>{item.assetLabel || item.assetId}</strong>
                  <span>{monitoringActionLabel(item.action)}</span>
                </header>
                <dl>
                  <div><dt>当前动作</dt><dd>{monitoringActionLabel(item.action)}</dd></div>
                  <div><dt>意图动作</dt><dd>{monitoringActionLabel(item.intendedAction)}</dd></div>
                  <div><dt>执行状态</dt><dd>{item.executionStatus === "blocked" ? "风险已触发，当前暂不可执行" : "未执行"}</dd></div>
                  <div><dt>持有周期</dt><dd>{item.plannedHorizonDays} 个交易日</dd></div>
                  <div><dt>结构方向</dt><dd>{item.structureDirection}</dd></div>
                  <div><dt>保护位</dt><dd>{item.protectivePrice ?? "—"}</dd></div>
                  <div><dt>板块阶段</dt><dd>{item.sectorRegime}</dd></div>
                  <div><dt>T+1 可卖</dt><dd>{item.sellableQuantity}</dd></div>
                </dl>
                {item.reasonCodes.length ? <p className="position-monitor-reasons">原因：{item.reasonCodes.join("、")}</p> : null}
              </article>
            ))}
          </div>
        ) : (
          <p className="empty-copy">{monitorModel.emptyText}</p>
        )}
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
          <Select
            value={values.orderDraftId || undefined}
            onChange={(value) => onSelectDraft(value ?? "")}
            options={[
              { value: "", label: "自主登记或暂不关联" },
              ...drafts.map((draft) => ({
                value: draft.orderDraftId,
                label: `${draft.assetLabel} · ${draft.actionLabel} · ${draft.priceRangeDisplay}`,
              })),
            ]}
            showSearch
            optionFilterProp="label"
          />
        </label>
        <label>
          <span>组合 ID</span>
          <Input value={values.portfolioId} onChange={(event) => onChange("portfolioId", event.target.value)} />
        </label>
        <label>
          <span>资产 ID</span>
          <Input placeholder="ashare:600519" value={values.assetId} onChange={(event) => onChange("assetId", event.target.value)} />
        </label>
        <label>
          <span>市场</span>
          <Input value={values.market} onChange={(event) => onChange("market", event.target.value)} />
        </label>
        <label>
          <span>动作</span>
          <Select
            value={values.action}
            onChange={(value) => onChange("action", value)}
            options={[
              { value: "buy", label: "买入" },
              { value: "add", label: "加仓" },
              { value: "sell", label: "卖出" },
              { value: "reduce", label: "减仓" },
            ]}
          />
        </label>
        <label>
          <span>执行价格</span>
          <Input value={values.executedPrice} onChange={(event) => onChange("executedPrice", event.target.value)} />
        </label>
        <label>
          <span>执行数量</span>
          <Input value={values.executedQuantity} onChange={(event) => onChange("executedQuantity", event.target.value)} />
        </label>
        <label>
          <span>执行时间</span>
          <Input placeholder="2026-06-13T10:05:00+08:00" value={values.executedAt} onChange={(event) => onChange("executedAt", event.target.value)} />
        </label>
        <label>
          <span>费用</span>
          <Input value={values.fee} onChange={(event) => onChange("fee", event.target.value)} />
        </label>
        <label className="action-loop-form-wide">
          <span>备注</span>
          <Input.TextArea value={values.note} onChange={(event) => onChange("note", event.target.value)} />
        </label>
      </div>
      <Button type="primary" disabled={disabled} loading={saving} onClick={onSubmit}>
        {saving ? "保存中" : "保存执行登记"}
      </Button>
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
