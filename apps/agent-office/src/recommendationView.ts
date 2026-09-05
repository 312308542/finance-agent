export type RecommendationTone = "green" | "amber" | "blue" | "red" | "muted";
export type DecisionFeedbackType = "accepted" | "rejected" | "modified" | "deferred";

export type PendingDecisionModel = {
  decisionId: string;
  assetId: string;
  sourceRecommendationId: string;
  suggestedAction: string;
  summary: string;
  reviewStatus: string;
};

export type ScoreBreakdownItem = {
  group: string;
  value: number;
  tone: RecommendationTone;
};

export type StructureEvidenceItem = {
  horizon: string;
  title: string;
  status: string;
  statusLabel: string;
  tone: RecommendationTone;
  confidence: number;
  confidenceDisplay: string;
  summary: string;
  evidenceId: string;
  invalidationPrice: string;
  confirmationPrice: string;
  isDemo: boolean;
};

export type RecommendationItemModel = {
  recommendationId: string;
  runId: string;
  assetId: string;
  symbol: string;
  name: string;
  assetLabel: string;
  market: string;
  marketLabel: string;
  action: string;
  actionLabel: string;
  actionTone: RecommendationTone;
  rank: number;
  totalScore: number;
  scoreDisplay: string;
  confidence: number;
  confidenceDisplay: string;
  riskCount: number;
  evidenceCount: number;
  summary: string;
  scoreBreakdown: ScoreBreakdownItem[];
  structureEvidence: StructureEvidenceItem[];
  structureStatus: string;
  isDemo: boolean;
  riskRebuttal: string;
  reportWorkflowRunId: string;
  pendingDecision: PendingDecisionModel | null;
  ownerId: string;
  recommendationState: string;
  previousState: string;
  stateChangedAt: string;
  decisionSnapshotId: string;
  plannedHorizonDays: number;
  sectorRegime: string;
  structureVerdict: Record<string, unknown>;
  entryZone: { low: number; high: number } | null;
  invalidationPrice: number | null;
  expectedNetReturn: number | null;
  downsideRisk: number | null;
  replacementReason: string;
  dataQuality: string;
  payload: Record<string, any>;
};

export type RecommendationRunModel = {
  runId: string;
  market: string;
  marketLabel: string;
  strategy: string;
  status: string;
  startedAt: string;
  payload: Record<string, any>;
};

export type RecommendationPageModel = {
  status: string;
  message: string;
  dataSource: string;
  isOfflineDemo: boolean;
  selectedMarket: string;
  marketTabs: Array<{ id: string; label: string; count: number }>;
  runs: RecommendationRunModel[];
  activeRun: RecommendationRunModel | null;
  items: RecommendationItemModel[];
  allItems: RecommendationItemModel[];
  lifecycleGroups: Record<LifecycleGroupKey, RecommendationItemModel[]>;
  pendingDecisions: PendingDecisionModel[];
  avoidPoolSummary: {
    count: number;
    description: string;
    assets: Array<Record<string, any>>;
  };
  metrics: {
    recommendationCount: number;
    buyCount: number;
    watchCount: number;
    buyReadyCount: number;
    activeCount: number;
    exitPendingCount: number;
    pendingDecisionCount: number;
  };
  emptyText: string;
};

export type LifecycleGroupKey =
  | "new_opportunities"
  | "continuing"
  | "waiting_entry"
  | "positions"
  | "weakening_or_exit";

export const lifecycleGroupLabels: Record<LifecycleGroupKey, string> = {
  new_opportunities: "今日新机会",
  continuing: "持续有效",
  waiting_entry: "等待入场",
  positions: "当前持仓建议",
  weakening_or_exit: "转弱与退出",
};

const marketLabels: Record<string, string> = {
  ashare: "A 股",
  crypto_spot: "数字货币现货",
  crypto_future: "数字货币合约",
};

const actionMeta: Record<string, { label: string; tone: RecommendationTone }> = {
  buy_candidate: { label: "候选待确认", tone: "blue" },
  buy: { label: "候选买入", tone: "green" },
  strong_buy: { label: "强候选买入", tone: "green" },
  watch: { label: "建议观察", tone: "blue" },
  hold: { label: "建议持有观察", tone: "blue" },
  wait: { label: "建议等待", tone: "amber" },
  wait_for_pullback: { label: "建议等待回撤", tone: "amber" },
  avoid: { label: "建议回避", tone: "red" },
  reject: { label: "建议回避", tone: "red" },
  sell: { label: "候选减仓", tone: "red" },
  reduce: { label: "建议减仓", tone: "red" },
  exit: { label: "建议退出", tone: "red" },
};

export function formatMarketLabel(market: string | null | undefined): string {
  const normalized = normalizeText(market);
  return (marketLabels[normalized] ?? normalized) || "未分市场";
}

export function actionLabel(action: string | null | undefined): string {
  const normalized = normalizeText(action).toLowerCase();
  return actionMeta[normalized]?.label ?? `建议${normalized || "观察"}`;
}

export function buildRecommendationPageModel(
  recommendationPayload: Record<string, any> | null | undefined,
  decisionsPayload: Record<string, any> | null | undefined,
  selectedMarket: string | null | undefined,
): RecommendationPageModel {
  const pendingDecisions = normalizePendingDecisions(decisionsPayload);
  const dataSource = normalizeText(recommendationPayload?.data_source || recommendationPayload?.dataSource);
  const isOfflineDemo = dataSource === "offline_demo";
  const allItems = normalizeRecommendationItems(recommendationPayload, pendingDecisions, isOfflineDemo);
  const runs = normalizeRecommendationRuns(recommendationPayload);
  const marketTabs = buildMarketTabs(allItems, runs);
  const fallbackMarket = marketTabs[0]?.id ?? "ashare";
  const currentMarket = selectedMarket && marketTabs.some((tab) => tab.id === selectedMarket)
    ? selectedMarket
    : fallbackMarket;
  const items = allItems.filter((item) => item.market === currentMarket);
  const lifecycleGroups = buildLifecycleGroups(items, recommendationPayload?.groups);
  const activeRuns = Array.isArray(recommendationPayload?.active_runs)
    ? recommendationPayload.active_runs
        .map((item: unknown) => normalizeRecommendationRun(item))
        .filter((item: RecommendationRunModel | null): item is RecommendationRunModel => Boolean(item))
    : [];
  const activeRun =
    activeRuns.find((run) => run.market === currentMarket) ??
    normalizeRecommendationRun(recommendationPayload?.active_run) ??
    runs.find((run) => run.market === currentMarket) ??
    runs[0] ??
    null;
  const avoidPoolSummary = summarizeAvoidPool(activeRun?.payload);
  const metrics = isRecord(recommendationPayload?.metrics) ? recommendationPayload.metrics : {};
  return {
    status: normalizeText(recommendationPayload?.status) || "empty",
    message: normalizeText(recommendationPayload?.message),
    dataSource,
    isOfflineDemo,
    selectedMarket: currentMarket,
    marketTabs,
    runs,
    activeRun,
    items,
    allItems,
    lifecycleGroups,
    pendingDecisions,
    avoidPoolSummary,
    metrics: {
      recommendationCount: normalizeNumber(metrics.recommendation_count ?? allItems.length),
      buyCount: normalizeNumber(metrics.buy_count ?? allItems.filter((item) => item.action.includes("buy")).length),
      watchCount: normalizeNumber(metrics.watch_count ?? allItems.filter((item) => item.action === "watch").length),
      buyReadyCount: normalizeNumber(
        metrics.buy_ready_count ?? allItems.filter((item) => item.recommendationState === "buy_ready").length,
      ),
      activeCount: normalizeNumber(
        metrics.active_count ?? allItems.filter((item) => item.recommendationState === "active").length,
      ),
      exitPendingCount: normalizeNumber(
        metrics.exit_pending_count ?? allItems.filter((item) => item.recommendationState === "exit_pending").length,
      ),
      pendingDecisionCount: pendingDecisions.length,
    },
    emptyText: "暂无推荐运行，等待推荐流水线生成候选建议。",
  };
}

export function normalizeRecommendationItem(
  item: unknown,
  pendingDecisions: PendingDecisionModel[] = [],
  isOfflineDemo = false,
): RecommendationItemModel | null {
  if (!isRecord(item)) {
    return null;
  }
  const recommendationId = normalizeText(item.recommendation_id || item.recommendationId);
  const symbol = normalizeText(item.symbol);
  const market = normalizeText(item.market);
  if (!recommendationId && !symbol) {
    return null;
  }
  const action = normalizeText(item.action).toLowerCase();
  const meta = actionMeta[action] ?? { label: actionLabel(action), tone: "blue" as RecommendationTone };
  const payload = isRecord(item.payload) ? item.payload : {};
  const isDemo =
    isOfflineDemo ||
    normalizeText(item.data_source || item.dataSource) === "offline_demo" ||
    normalizeText(payload.data_source || payload.dataSource) === "offline_demo";
  const structureEvidence = normalizeStructureEvidence(payload, { isOfflineDemo: isDemo });
  const riskIds = Array.isArray(item.risk_ids) ? item.risk_ids : [];
  const evidenceIds = Array.isArray(item.evidence_ids) ? item.evidence_ids : [];
  const score = normalizeNumber(item.total_score);
  const confidence = normalizeNumber(item.confidence);
  const pendingDecision =
    pendingDecisions.find((decision) => {
      if (decision.sourceRecommendationId && recommendationId) {
        return decision.sourceRecommendationId === recommendationId;
      }
      return decision.assetId && decision.assetId === normalizeText(item.asset_id);
    }) ?? null;
  return {
    recommendationId,
    runId: normalizeText(item.run_id),
    assetId: normalizeText(item.asset_id),
    symbol,
    name: normalizeText(item.name),
    assetLabel: `${symbol}${item.name ? ` ${normalizeText(item.name)}` : ""}`.trim(),
    market,
    marketLabel: formatMarketLabel(market),
    action,
    actionLabel: meta.label,
    actionTone: meta.tone,
    rank: normalizeNumber(item.rank),
    totalScore: score,
    scoreDisplay: formatScore(score),
    confidence,
    confidenceDisplay: formatPercent(confidence),
    riskCount: riskIds.length,
    evidenceCount: evidenceIds.length,
    summary: normalizeText(item.summary),
    scoreBreakdown: normalizeScoreBreakdown(payload.score_breakdown || payload.scoreBreakdown),
    structureEvidence,
    structureStatus: normalizeStructureStatus(payload, structureEvidence),
    isDemo,
    riskRebuttal: normalizeText(payload.risk_rebuttal || payload.riskRebuttal || payload.risk_summary),
    reportWorkflowRunId: normalizeText(payload.workflow_run_id || payload.report_workflow_run_id),
    pendingDecision,
    ownerId: normalizeText(item.owner_id || item.ownerId || payload.owner_id || payload.ownerId),
    recommendationState: normalizeText(
      item.recommendation_state || item.recommendationState || payload.recommendation_state || payload.recommendationState,
    ) || "watch",
    previousState: normalizeText(item.previous_state || item.previousState || payload.previous_state || payload.previousState),
    stateChangedAt: normalizeText(item.state_changed_at || item.stateChangedAt || payload.state_changed_at || payload.stateChangedAt),
    decisionSnapshotId: normalizeText(
      item.decision_snapshot_id || item.decisionSnapshotId || payload.decision_snapshot_id || payload.decisionSnapshotId,
    ),
    plannedHorizonDays: normalizeNumber(item.planned_horizon_days || item.plannedHorizonDays || payload.planned_horizon_days || payload.plannedHorizonDays),
    sectorRegime: normalizeText(item.sector_regime || item.sectorRegime || payload.sector_regime || payload.sectorRegime),
    structureVerdict: isRecord(item.structure_verdict || item.structureVerdict || payload.structure_verdict || payload.structureVerdict)
      ? (item.structure_verdict || item.structureVerdict || payload.structure_verdict || payload.structureVerdict)
      : {},
    entryZone: normalizeEntryZone(item.entry_zone || item.entryZone || payload.entry_zone || payload.entryZone),
    invalidationPrice: normalizeNullableNumber(item.invalidation_price ?? item.invalidationPrice ?? payload.invalidation_price ?? payload.invalidationPrice),
    expectedNetReturn: normalizeNullableNumber(item.expected_net_return ?? item.expectedNetReturn ?? payload.expected_net_return ?? payload.expectedNetReturn),
    downsideRisk: normalizeNullableNumber(item.downside_risk ?? item.downsideRisk ?? payload.downside_risk ?? payload.downsideRisk),
    replacementReason: normalizeText(item.replacement_reason || item.replacementReason || payload.replacement_reason || payload.replacementReason),
    dataQuality: normalizeText(item.data_quality || item.dataQuality || payload.data_quality || payload.dataQuality),
    payload,
  };
}

function buildLifecycleGroups(
  items: RecommendationItemModel[],
  rawGroups: unknown,
): Record<LifecycleGroupKey, RecommendationItemModel[]> {
  const groups: Record<LifecycleGroupKey, RecommendationItemModel[]> = {
    new_opportunities: [],
    continuing: [],
    waiting_entry: [],
    positions: [],
    weakening_or_exit: [],
  };
  const source = isRecord(rawGroups) ? rawGroups : null;
  if (source) {
    for (const key of Object.keys(groups) as LifecycleGroupKey[]) {
      const rows = Array.isArray(source[key]) ? source[key] : [];
      const ids = new Set(rows.filter(isRecord).map((row) => normalizeText(row.recommendation_id || row.recommendationId)));
      groups[key] = items.filter((item) => ids.has(item.recommendationId));
    }
    if (Object.values(groups).some((group) => group.length)) {
      return groups;
    }
  }
  for (const item of items) {
    const state = item.recommendationState;
    const key: LifecycleGroupKey =
      state === "active"
        ? "positions"
        : state === "discovered" || state === "buy_ready"
          ? "new_opportunities"
          : state === "setup_confirming" || state === "watch"
            ? "waiting_entry"
            : state === "weakening" || state === "exit_pending" || state === "exited" || state === "cooldown"
              ? "weakening_or_exit"
              : "continuing";
    groups[key].push(item);
  }
  return groups;
}

function normalizeEntryZone(value: unknown): { low: number; high: number } | null {
  if (!isRecord(value)) {
    return null;
  }
  const low = Number(value.low ?? value.min ?? value["0"]);
  const high = Number(value.high ?? value.max ?? value["1"]);
  return Number.isFinite(low) && Number.isFinite(high) ? { low, high } : null;
}

function normalizeNullableNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function buildDecisionFeedbackPayload(
  feedback: DecisionFeedbackType,
  comment: string,
  modifiedAction?: string | null,
): Record<string, string> {
  const payload: Record<string, string> = { feedback };
  const cleanComment = comment.trim();
  if (cleanComment) {
    payload.comment = cleanComment;
  }
  if (feedback === "modified") {
    const cleanAction = normalizeText(modifiedAction);
    if (cleanAction) {
      payload.modified_action = cleanAction;
    }
  }
  return payload;
}

export function mergeRecommendationPayloads(payloads: Array<Record<string, any> | null | undefined>): Record<string, any> {
  const validPayloads = payloads.filter((payload): payload is Record<string, any> => isRecord(payload));
  const runs = validPayloads.flatMap((payload) => (Array.isArray(payload.runs) ? payload.runs : []));
  const recommendations = validPayloads.flatMap((payload) =>
    Array.isArray(payload.recommendations) ? payload.recommendations : [],
  );
  const activeRuns = validPayloads
    .map((payload) => payload.active_run)
    .filter(isRecord);
  const metrics = validPayloads.reduce(
    (acc, payload) => {
      const source = isRecord(payload.metrics) ? payload.metrics : {};
      acc.recommendation_count += normalizeNumber(source.recommendation_count);
      acc.buy_count += normalizeNumber(source.buy_count);
      acc.watch_count += normalizeNumber(source.watch_count);
      return acc;
    },
    { recommendation_count: 0, buy_count: 0, watch_count: 0 },
  );
  const message = validPayloads
    .map((payload) => normalizeText(payload.message))
    .find((value) => Boolean(value)) ?? "";
  return {
    status: recommendations.length ? "ok" : validPayloads.some((payload) => payload.status === "unavailable") ? "unavailable" : "empty",
    data_source: validPayloads.some((payload) => payload.data_source === "offline_demo") ? "offline_demo" : undefined,
    runs,
    active_runs: activeRuns,
    active_run: activeRuns[0] ?? null,
    recommendations,
    message,
    metrics,
  };
}

function normalizeRecommendationItems(
  payload: Record<string, any> | null | undefined,
  pendingDecisions: PendingDecisionModel[],
  isOfflineDemo: boolean,
): RecommendationItemModel[] {
  const rows = Array.isArray(payload?.recommendations) ? payload.recommendations : [];
  return rows
    .map((item) => normalizeRecommendationItem(item, pendingDecisions, isOfflineDemo))
    .filter((item): item is RecommendationItemModel => Boolean(item));
}

function normalizeRecommendationRuns(payload: Record<string, any> | null | undefined): RecommendationRunModel[] {
  const rows = Array.isArray(payload?.runs) ? payload.runs : [];
  return rows
    .map((item) => normalizeRecommendationRun(item))
    .filter((item): item is RecommendationRunModel => Boolean(item));
}

function normalizeRecommendationRun(item: unknown): RecommendationRunModel | null {
  if (!isRecord(item)) {
    return null;
  }
  const runId = normalizeText(item.run_id || item.runId);
  if (!runId) {
    return null;
  }
  const market = normalizeText(item.market);
  return {
    runId,
    market,
    marketLabel: formatMarketLabel(market),
    strategy: normalizeText(item.strategy),
    status: normalizeText(item.status),
    startedAt: normalizeText(item.started_at || item.finished_at),
    payload: isRecord(item.payload) ? item.payload : {},
  };
}

function normalizePendingDecisions(payload: Record<string, any> | null | undefined): PendingDecisionModel[] {
  const data = isRecord(payload?.data) ? payload.data : payload;
  const rows = Array.isArray(data?.items) ? data.items : [];
  return rows
    .map((item) => {
      if (!isRecord(item)) {
        return null;
      }
      const decisionId = normalizeText(item.decision_id || item.decisionId);
      if (!decisionId) {
        return null;
      }
      return {
        decisionId,
        assetId: normalizeText(item.asset_id),
        sourceRecommendationId: normalizeText(item.source_recommendation_id),
        suggestedAction: normalizeText(item.suggested_action),
        summary: normalizeText(item.summary),
        reviewStatus: normalizeText(item.review_status || item.user_action),
      };
    })
    .filter((item): item is PendingDecisionModel => Boolean(item));
}

function buildMarketTabs(items: RecommendationItemModel[], runs: RecommendationRunModel[]) {
  const markets = new Map<string, number>();
  items.forEach((item) => markets.set(item.market, (markets.get(item.market) ?? 0) + 1));
  runs.forEach((run) => {
    if (!markets.has(run.market)) {
      markets.set(run.market, 0);
    }
  });
  return Array.from(markets.entries())
    .filter(([market]) => Boolean(market))
    .map(([market, count]) => ({
      id: market,
      label: formatMarketLabel(market),
      count,
    }));
}

function summarizeAvoidPool(payload: Record<string, any> | null | undefined) {
  const source = isRecord(payload?.avoid_pool_excluded) ? payload.avoid_pool_excluded : {};
  const assets = Array.isArray(source.assets) ? source.assets.filter(isRecord) : [];
  const count = normalizeNumber(source.count ?? assets.length);
  return {
    count,
    description: count > 0 ? `本次运行剔除回避池 ${count} 项，已从候选建议中排除。` : "本次运行未发现回避池剔除项。",
    assets,
  };
}

export function normalizeStructureEvidence(
  value: unknown,
  options: { isOfflineDemo?: boolean } = {},
): StructureEvidenceItem[] {
  const source = resolveStructureSource(value);
  const frames = Array.isArray(source?.structure_frames)
    ? source.structure_frames
    : Array.isArray(source?.frames)
      ? source.frames
      : [];
  return frames
    .map((frame) => normalizeStructureFrame(frame, options))
    .filter((item): item is StructureEvidenceItem => Boolean(item));
}

export function normalizeStructureStatus(
  value: unknown,
  evidence: StructureEvidenceItem[] = [],
): string {
  const source = resolveStructureSource(value);
  const status = normalizeText(source?.status);
  if (status) {
    return status;
  }
  if (evidence.length) {
    return "available";
  }
  return "";
}

function resolveStructureSource(value: unknown): Record<string, any> | null {
  if (!isRecord(value)) {
    return null;
  }
  for (const key of ["structure", "structural_lite", "structuralLite", "structureEvidence"]) {
    const candidate = value[key];
    if (isRecord(candidate)) {
      return candidate;
    }
  }
  return value;
}

function normalizeStructureFrame(
  frame: unknown,
  options: { isOfflineDemo?: boolean },
): StructureEvidenceItem | null {
  if (!isRecord(frame)) {
    return null;
  }
  const payload = isRecord(frame.payload) ? frame.payload : frame;
  const horizon = normalizeText(frame.horizon || payload.schema_version || payload.horizon);
  if (!horizon) {
    return null;
  }
  const status = normalizeText(frame.status || payload.status) || "unknown";
  const confidence = normalizeStructureConfidence(payload, frame);
  const evidenceId = normalizeText(payload.evidence_id || frame.evidence_id);
  return {
    horizon,
    title: structureTitle(horizon),
    status,
    statusLabel: structureStatusLabel(status),
    tone: structureTone(status),
    confidence,
    confidenceDisplay: formatPercent(confidence),
    summary: structureSummary(horizon, payload),
    evidenceId,
    invalidationPrice: normalizeStructurePrice(
      firstRecord(payload.patterns)?.invalidation_price ??
        firstRecord(payload.candidates)?.thesis_invalidation_price ??
        firstRecord(payload.candidates)?.invalidation_price,
    ),
    confirmationPrice: normalizeStructurePrice(
      firstRecord(payload.candidates)?.thesis_confirmation_price,
    ),
    isDemo: Boolean(options.isOfflineDemo) || evidenceId.includes(":mock"),
  };
}

function normalizeStructureConfidence(payload: Record<string, any>, frame: Record<string, any>): number {
  const direct = normalizeNumber(payload.confidence ?? frame.confidence);
  if (direct > 0) {
    return direct;
  }
  const patternConfidence = normalizeNumber(firstRecord(payload.patterns)?.confidence);
  if (patternConfidence > 0) {
    return patternConfidence;
  }
  const candidateConfidence = normalizeNumber(firstRecord(payload.candidates)?.confidence);
  return candidateConfidence;
}

function structureTitle(horizon: string): string {
  if (horizon.includes("smc")) {
    return "SMC 结构";
  }
  if (horizon.includes("harmonic")) {
    return "谐波结构";
  }
  if (horizon.includes("elliott")) {
    return "波浪结构";
  }
  if (horizon.includes("swing")) {
    return "摆动结构";
  }
  return "结构证据";
}

function structureStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    available: "可用",
    no_pattern: "无形态",
    no_structure_event: "无结构事件",
    insufficient_structure: "结构不足",
    insufficient_data: "数据不足",
    error: "异常",
  };
  return labels[status] ?? (status || "未知");
}

function structureTone(status: string): RecommendationTone {
  if (status === "available") {
    return "green";
  }
  if (status === "error") {
    return "red";
  }
  if (status.startsWith("insufficient")) {
    return "amber";
  }
  return "blue";
}

function structureSummary(horizon: string, payload: Record<string, any>): string {
  if (horizon.includes("smc")) {
    const eventCount = Array.isArray(payload.structure_events) ? payload.structure_events.length : 0;
    const gapCount = Array.isArray(payload.fair_value_gaps) ? payload.fair_value_gaps.length : 0;
    const firstEvent = firstRecord(payload.structure_events);
    const eventName = normalizeText(firstEvent?.name).replace("bos", "BOS").replace("choch", "CHoCH");
    return eventName
      ? `${eventName}，结构事件 ${eventCount} 个，FVG ${gapCount} 个`
      : `结构事件 ${eventCount} 个，FVG ${gapCount} 个`;
  }
  if (horizon.includes("harmonic")) {
    const pattern = firstRecord(payload.patterns);
    if (pattern) {
      const direction = normalizeText(pattern.direction) === "bearish" ? "看空" : "看多";
      return `${direction} ${normalizeText(pattern.pattern) || "形态"}，失效价 ${normalizeStructurePrice(pattern.invalidation_price) || "未给出"}`;
    }
  }
  if (horizon.includes("elliott")) {
    const candidate = firstRecord(payload.candidates);
    if (candidate) {
      return `${normalizeText(candidate.pattern) || "候选浪型"}，信号 ${normalizeText(candidate.signal_hint) || "待确认"}`;
    }
  }
  if (horizon.includes("swing")) {
    const count = Array.isArray(payload.swings) ? payload.swings.length : 0;
    return `已确认摆动点 ${count} 个`;
  }
  return structureStatusLabel(normalizeText(payload.status));
}

function firstRecord(value: unknown): Record<string, any> | null {
  return Array.isArray(value) && isRecord(value[0]) ? value[0] : null;
}

function normalizeStructurePrice(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  const numeric = Number(value);
  return Number.isFinite(numeric) ? String(numeric) : normalizeText(value);
}

function normalizeScoreBreakdown(value: unknown): ScoreBreakdownItem[] {
  if (!isRecord(value)) {
    return [];
  }
  return Object.entries(value).map(([group, raw]) => {
    const score = normalizeNumber(raw);
    return {
      group,
      value: score,
      tone: score < 0 ? "red" : score >= 20 ? "green" : "blue",
    };
  });
}

function formatScore(value: number): string {
  if (!Number.isFinite(value)) {
    return "—";
  }
  return value.toFixed(2).replace(/\.00$/, "");
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) {
    return "—";
  }
  const normalized = value <= 1 ? value * 100 : value;
  return `${Math.round(normalized)}%`;
}

function normalizeText(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value.trim();
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}

function normalizeNumber(value: unknown): number {
  const next = Number(value ?? 0);
  return Number.isFinite(next) ? next : 0;
}

function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
