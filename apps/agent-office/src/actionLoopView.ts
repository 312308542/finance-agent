export type OrderDraftModel = {
  orderDraftId: string;
  ownerId: string;
  portfolioId: string;
  assetId: string;
  assetLabel: string;
  market: string;
  decisionLogId: string;
  action: string;
  actionLabel: string;
  priceRangeDisplay: string;
  positionRatioDisplay: string;
  constraints: Record<string, any>;
  status: string;
  statusLabel: string;
  disclaimer: string;
  createdAt: string;
  updatedAt: string;
};

export type ExecutionRecordModel = {
  executionId: string;
  ownerId: string;
  portfolioId: string;
  assetId: string;
  assetLabel: string;
  market: string;
  orderDraftId: string;
  decisionLogId: string;
  action: string;
  actionLabel: string;
  priceDisplay: string;
  quantityDisplay: string;
  feeDisplay: string;
  executedAt: string;
  note: string;
  source: string;
  sourceLabel: string;
  createdAt: string;
};

export type UpcomingReviewModel = {
  reviewTaskId: string;
  ownerId: string;
  assetId: string;
  assetLabel: string;
  sourceDecisionId: string;
  reviewType: string;
  title: string;
  detail: string;
  dueAt: string;
  status: string;
  statusLabel: string;
  executionId: string;
};

export type ExecutionFormValues = {
  ownerId: string;
  portfolioId: string;
  assetId: string;
  market: string;
  action: string;
  executedPrice: string;
  executedQuantity: string;
  executedAt: string;
  orderDraftId?: string | null;
  decisionLogId?: string | null;
  fee?: string | null;
  note?: string | null;
};

const actionLabels: Record<string, string> = {
  buy: "买入",
  add: "加仓",
  sell: "卖出",
  reduce: "减仓",
};

const statusLabels: Record<string, string> = {
  drafted: "已生成",
  superseded: "已被新草案替代",
  cancelled: "已作废",
  pending: "待复盘",
  completed: "已复盘",
};

export function buildOrderDraftsModel(payload: Record<string, any> | null | undefined) {
  const items = asArray(payload?.data?.items ?? payload?.items)
    .map(normalizeOrderDraft)
    .filter(isPresent);
  return {
    status: normalizeText(payload?.status) || "empty",
    items,
    metrics: {
      draftedCount: items.filter((item) => item.status === "drafted").length,
      totalCount: items.length,
    },
    emptyText: "暂无订单草案，确认采纳建议后可生成文档性质操作草案。",
  };
}

export function buildExecutionsModel(payload: Record<string, any> | null | undefined) {
  const items = asArray(payload?.data?.items ?? payload?.items)
    .map(normalizeExecutionRecord)
    .filter(isPresent);
  return {
    status: normalizeText(payload?.status) || "empty",
    items,
    metrics: {
      totalCount: items.length,
      userReportedCount: items.filter((item) => item.source === "user_reported").length,
    },
    emptyText: "暂无执行登记，外部完成操作后可在这里记账并触发后续复盘。",
  };
}

export function buildUpcomingReviewsModel(payload: Record<string, any> | null | undefined) {
  const items = asArray(payload?.data?.items ?? payload?.items)
    .map(normalizeUpcomingReview)
    .filter(isPresent);
  return {
    status: normalizeText(payload?.status) || "empty",
    items,
    metrics: {
      pendingCount: items.filter((item) => item.status === "pending").length,
      totalCount: items.length,
    },
    emptyText: "暂无待复盘任务，执行登记后会自动生成后续回看提醒。",
  };
}

export function normalizeOrderDraft(item: unknown): OrderDraftModel | null {
  if (!isRecord(item)) {
    return null;
  }
  const orderDraftId = normalizeText(item.order_draft_id || item.orderDraftId);
  if (!orderDraftId) {
    return null;
  }
  const action = normalizeText(item.action).toLowerCase();
  const assetId = normalizeText(item.asset_id);
  const status = normalizeText(item.status);
  return {
    orderDraftId,
    ownerId: normalizeText(item.owner_id),
    portfolioId: normalizeText(item.portfolio_id),
    assetId,
    assetLabel: assetSymbol(assetId),
    market: normalizeText(item.market),
    decisionLogId: normalizeText(item.decision_log_id),
    action,
    actionLabel: actionLabels[action] ?? (action || "操作"),
    priceRangeDisplay: formatPriceRange(item.suggested_price_range),
    positionRatioDisplay: formatPercent(item.suggested_position_ratio),
    constraints: isRecord(item.constraints) ? item.constraints : {},
    status,
    statusLabel: statusLabels[status] ?? status,
    disclaimer: normalizeText(item.disclaimer),
    createdAt: normalizeText(item.created_at),
    updatedAt: normalizeText(item.updated_at),
  };
}

export function normalizeExecutionRecord(item: unknown): ExecutionRecordModel | null {
  if (!isRecord(item)) {
    return null;
  }
  const executionId = normalizeText(item.execution_id || item.executionId);
  if (!executionId) {
    return null;
  }
  const action = normalizeText(item.action).toLowerCase();
  const assetId = normalizeText(item.asset_id);
  const source = normalizeText(item.source);
  return {
    executionId,
    ownerId: normalizeText(item.owner_id),
    portfolioId: normalizeText(item.portfolio_id),
    assetId,
    assetLabel: assetSymbol(assetId),
    market: normalizeText(item.market),
    orderDraftId: normalizeText(item.order_draft_id),
    decisionLogId: normalizeText(item.decision_log_id),
    action,
    actionLabel: actionLabels[action] ?? (action || "操作"),
    priceDisplay: formatNumber(item.executed_price, 2, true),
    quantityDisplay: formatNumber(item.executed_quantity, 4),
    feeDisplay: formatNumber(item.fee, 2, true),
    executedAt: normalizeText(item.executed_at),
    note: normalizeText(item.note),
    source,
    sourceLabel: source === "user_reported" ? "用户手工登记" : source,
    createdAt: normalizeText(item.created_at),
  };
}

export function normalizeUpcomingReview(item: unknown): UpcomingReviewModel | null {
  if (!isRecord(item)) {
    return null;
  }
  const reviewTaskId = normalizeText(item.review_task_id || item.reviewTaskId);
  if (!reviewTaskId) {
    return null;
  }
  const assetId = normalizeText(item.asset_id);
  const status = normalizeText(item.status);
  const questions = asArray(item.review_questions)
    .map((question) => (isRecord(question) ? normalizeText(question.question) : normalizeText(question)))
    .filter(Boolean);
  const executionId = normalizeText(item.payload?.execution_id);
  return {
    reviewTaskId,
    ownerId: normalizeText(item.owner_id),
    assetId,
    assetLabel: assetSymbol(assetId),
    sourceDecisionId: normalizeText(item.source_decision_id),
    reviewType: normalizeText(item.review_type),
    title: `${assetSymbol(assetId)} 执行复盘`,
    detail: questions.join("；") || "比较建议、实际执行与后续表现。",
    dueAt: normalizeText(item.due_at),
    status,
    statusLabel: statusLabels[status] ?? status,
    executionId,
  };
}

export type ExecutionRecordPayloadModel = {
  owner_id: string;
  portfolio_id: string;
  asset_id: string;
  market: string;
  action: string;
  executed_price: string;
  executed_quantity: string;
  executed_at: string;
  source: "user_reported";
  order_draft_id?: string;
  decision_log_id?: string;
  fee?: string;
  note?: string;
};

export function buildExecutionPayload(values: ExecutionFormValues): ExecutionRecordPayloadModel {
  const payload: ExecutionRecordPayloadModel = {
    owner_id: values.ownerId,
    portfolio_id: values.portfolioId,
    asset_id: values.assetId,
    market: values.market,
    action: values.action,
    executed_price: values.executedPrice.trim(),
    executed_quantity: values.executedQuantity.trim(),
    executed_at: values.executedAt.trim(),
    source: "user_reported",
  };
  addOptional(payload, "order_draft_id", values.orderDraftId);
  addOptional(payload, "decision_log_id", values.decisionLogId);
  addOptional(payload, "fee", values.fee);
  addOptional(payload, "note", values.note);
  return payload;
}

function addOptional(
  payload: ExecutionRecordPayloadModel,
  key: "order_draft_id" | "decision_log_id" | "fee" | "note",
  value: string | null | undefined,
) {
  const clean = normalizeText(value);
  if (clean) {
    payload[key] = clean;
  }
}

function formatPriceRange(value: unknown): string {
  if (!isRecord(value)) {
    return "-";
  }
  const low = normalizeText(value.low);
  const high = normalizeText(value.high);
  if (!low || !high) {
    return low || high || "-";
  }
  return `${low} - ${high}`;
}

function formatPercent(value: unknown): string {
  const numeric = normalizeNumber(value);
  if (!numeric) {
    return "-";
  }
  return `${Math.round(numeric * 100)}%`;
}

function formatNumber(value: unknown, digits: number, fixed = false): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return normalizeText(value);
  }
  const text = numeric.toFixed(digits);
  return fixed ? text : text.replace(/\.?0+$/, "");
}

function assetSymbol(assetId: string): string {
  if (!assetId) {
    return "未知标的";
  }
  return assetId.includes(":") ? assetId.split(":").at(-1) || assetId : assetId;
}

function asArray(value: unknown): any[] {
  return Array.isArray(value) ? value : [];
}

function normalizeText(value: unknown): string {
  return value == null ? "" : String(value).trim();
}

function normalizeNumber(value: unknown): number {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
}

function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isPresent<T>(value: T | null | undefined | false): value is T {
  return Boolean(value);
}
