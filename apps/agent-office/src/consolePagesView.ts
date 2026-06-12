export type Tone = "green" | "amber" | "blue" | "red" | "muted";

export type LabeledValue = {
  label: string;
  value: number;
  display: string;
  tone: Tone;
};

const severityMeta: Record<string, { label: string; tone: Tone }> = {
  critical: { label: "严重", tone: "red" },
  high: { label: "高", tone: "red" },
  medium: { label: "中", tone: "amber" },
  low: { label: "低", tone: "blue" },
  unknown: { label: "未知", tone: "muted" },
};

const memoryTypeLabels: Record<string, string> = {
  decision_summary: "决策摘要",
  candidate_intake_reason: "入池原因",
  review_result: "复盘结论",
  user_feedback: "用户反馈",
  risk_preference: "风险偏好",
};

export function buildPortfolioPageModel(payload: Record<string, any> | null | undefined) {
  const metrics = isRecord(payload?.metrics) ? payload.metrics : {};
  const concentration = isRecord(metrics.concentration) ? metrics.concentration : {};
  const positions = asArray(payload?.positions).map(normalizePositionItem).filter(isPresent);
  const concentrationWarnings = asArray(payload?.concentration_warnings)
    .map(normalizeConcentrationWarning)
    .filter(isPresent);
  return {
    status: normalizeText(payload?.status) || "empty",
    activePortfolioId: normalizeText(payload?.active_portfolio_id),
    portfolios: asArray(payload?.portfolios),
    positions,
    concentrationWarnings,
    marketWeights: weightsToList(concentration.market_weights),
    sectorWeights: weightsToList(concentration.sector_weights),
    industryWeights: weightsToList(concentration.industry_weights),
    metrics: {
      positionCount: normalizeNumber(metrics.position_count ?? positions.length),
      positivePositionCount: normalizeNumber(metrics.positive_position_count),
      negativePositionCount: normalizeNumber(metrics.negative_position_count),
      riskProfile: normalizeText(metrics.risk_profile) || "未配置",
      maxPositionWeight: normalizeNumber(concentration.max_position_weight),
      maxPositionWeightDisplay: formatPercent(concentration.max_position_weight),
      overPositionThresholdCount: normalizeNumber(concentration.over_position_threshold_count),
    },
    emptyText: "暂无持仓数据，配置组合后可查看集中度、盈亏和风险提示。",
  };
}

export function buildRiskPageModel(payload: Record<string, any> | null | undefined) {
  const metrics = isRecord(payload?.metrics) ? payload.metrics : {};
  const severityBreakdown = buildSeverityBreakdown(metrics.risk_severity_breakdown);
  const alerts = asArray(payload?.alerts).map(normalizeRiskEvent("monitoring_alert")).filter(isPresent);
  const triggers = asArray(payload?.triggers).map(normalizeRiskEvent("trigger_event")).filter(isPresent);
  const events = [...alerts, ...triggers].sort((a, b) => b.timestamp.localeCompare(a.timestamp));
  const findings = asArray(payload?.risk_findings).map(normalizeRiskFinding).filter(isPresent);
  const dataQuality = asArray(payload?.data_quality).map(normalizeDataQuality).filter(isPresent);
  return {
    status: normalizeText(payload?.status) || "empty",
    events,
    findings,
    dataQuality,
    severityBreakdown,
    metrics: {
      alertCount: normalizeNumber(metrics.alert_count ?? alerts.length),
      triggerCount: normalizeNumber(metrics.trigger_count ?? triggers.length),
      riskFindingCount: normalizeNumber(metrics.risk_finding_count ?? findings.length),
      highSeverityCount: normalizeNumber(metrics.high_severity_count),
      dataIssueCount: normalizeNumber(metrics.data_issue_count),
    },
    emptyText: "暂无风险提醒，系统会在触发事件、风险发现或数据质量异常出现后展示。",
  };
}

export function buildAgentPageModel(payload: Record<string, any> | null | undefined) {
  const metrics = isRecord(payload?.metrics) ? payload.metrics : {};
  const runs = asArray(payload?.runs).map(normalizeWorkflowRun).filter(isPresent);
  return {
    status: normalizeText(payload?.status) || "empty",
    available: asArray(payload?.available),
    runs,
    metrics: {
      availableCount: asArray(payload?.available).length,
      recentCount: normalizeNumber(metrics.recent_count ?? runs.length),
      runningCount: normalizeNumber(metrics.running_count),
      failedCount: normalizeNumber(metrics.failed_count),
    },
    emptyText: "暂无 Workflow 运行审计，触发报告、推荐或持仓监控后会出现在这里。",
  };
}

export function buildMemoryPageModel(payload: Record<string, any> | null | undefined) {
  const metrics = isRecord(payload?.metrics) ? payload.metrics : {};
  const sourceItems = asArray(payload?.items).length ? asArray(payload?.items) : asArray(payload?.memories);
  const items = sourceItems.map(normalizeMemoryItem).filter(isPresent);
  return {
    status: normalizeText(payload?.status) || "empty",
    items,
    metrics: {
      memoryCount: normalizeNumber(metrics.memory_count ?? items.length),
      staleMemoryCount: normalizeNumber(metrics.stale_memory_count),
      assetCount: normalizeNumber(metrics.asset_count),
    },
    emptyText: "暂无 Finance Memory，用户反馈、复盘结论和长期偏好会沉淀在这里。",
  };
}

export function formatSeverityLabel(severity: string | null | undefined): string {
  const normalized = normalizeText(severity).toLowerCase();
  return severityMeta[normalized]?.label ?? (normalized || "未知");
}

function normalizePositionItem(item: unknown) {
  if (!isRecord(item)) {
    return null;
  }
  const symbol = normalizeText(item.symbol);
  const name = normalizeText(item.name || item.payload?.name);
  return {
    assetId: normalizeText(item.asset_id),
    symbol,
    name,
    assetLabel: `${symbol}${name ? ` ${name}` : ""}`.trim() || normalizeText(item.asset_id),
    market: normalizeText(item.market),
    side: normalizeText(item.side),
    marketValue: normalizeText(item.market_value),
    unrealizedPnl: normalizeText(item.unrealized_pnl),
    unrealizedPnlPctDisplay: formatPercent(item.unrealized_pnl_pct),
    weightDisplay: formatPercent(item.portfolio_weight),
    industry: normalizeText(item.payload?.industry || "未分类"),
    status: normalizeText(item.status),
  };
}

function normalizeConcentrationWarning(item: unknown) {
  if (!isRecord(item)) {
    return null;
  }
  return {
    type: normalizeText(item.type),
    assetId: normalizeText(item.asset_id),
    symbol: normalizeText(item.symbol),
    weightDisplay: formatPercent(item.weight),
    thresholdDisplay: formatPercent(item.threshold),
    message: normalizeText(item.message),
  };
}

function normalizeRiskEvent(itemType: "monitoring_alert" | "trigger_event") {
  return (item: unknown) => {
    if (!isRecord(item)) {
      return null;
    }
    const severity = normalizeText(item.severity).toLowerCase() || "unknown";
    const timestamp = normalizeText(itemType === "monitoring_alert" ? item.as_of : item.triggered_at);
    return {
      id: normalizeText(item.alert_id || item.trigger_event_id),
      itemType,
      title: normalizeText(item.trigger_condition || item.alert_type || item.trigger_ref || item.trigger_type),
      severity,
      severityLabel: formatSeverityLabel(severity),
      tone: severityMeta[severity]?.tone ?? "muted",
      status: normalizeText(item.status),
      timestamp,
      detail: normalizeText(item.payload?.summary || item.payload?.reason || item.requested_workflow_type),
      workflowRunId: normalizeText(item.payload?.workflow_run_id),
    };
  };
}

function normalizeRiskFinding(item: unknown) {
  if (!isRecord(item)) {
    return null;
  }
  const severity = normalizeText(item.severity).toLowerCase() || "unknown";
  return {
    riskId: normalizeText(item.risk_id),
    assetId: normalizeText(item.asset_id),
    riskType: normalizeText(item.risk_type),
    title: normalizeText(item.title || item.risk_type),
    description: normalizeText(item.description),
    severity,
    severityLabel: formatSeverityLabel(severity),
    tone: severityMeta[severity]?.tone ?? "muted",
    scoreDisplay: formatScore(item.score),
    asOf: normalizeText(item.as_of),
  };
}

function normalizeDataQuality(item: unknown) {
  if (!isRecord(item)) {
    return null;
  }
  return {
    qualityId: normalizeText(item.quality_id),
    title: `${normalizeText(item.market)} / ${normalizeText(item.data_domain)}`.trim(),
    provider: normalizeText(item.provider),
    status: normalizeText(item.status),
    issueCount: normalizeNumber(item.issue_count),
    latestDataAt: normalizeText(item.latest_data_at),
  };
}

function normalizeWorkflowRun(item: unknown) {
  if (!isRecord(item)) {
    return null;
  }
  const payload = isRecord(item.payload) ? item.payload : {};
  return {
    workflowRunId: normalizeText(item.workflow_run_id),
    workflowType: normalizeText(item.workflow_type),
    triggerType: normalizeText(item.trigger_type),
    triggerRef: normalizeText(item.trigger_ref),
    status: normalizeText(item.status),
    startedAt: normalizeText(item.started_at),
    finishedAt: normalizeText(item.finished_at),
    durationDisplay: formatDuration(item.started_at, item.finished_at),
    modelSourceDisplay: formatModelSource(payload.model_roundtable?.generated_by),
    reviewStatus: normalizeText(payload.review_status || payload.report?.review_status?.status),
    payload,
  };
}

function normalizeMemoryItem(item: unknown) {
  if (!isRecord(item)) {
    return null;
  }
  const memoryType = normalizeText(item.memory_type);
  return {
    memoryId: normalizeText(item.memory_id),
    memoryType,
    typeLabel: memoryTypeLabels[memoryType] ?? (memoryType || "记忆"),
    scope: normalizeText(item.scope),
    assetId: normalizeText(item.asset_id),
    content: normalizeText(item.content),
    confidenceDisplay: formatPercent(item.confidence),
    status: normalizeText(item.status),
    createdAt: normalizeText(item.created_at),
    workflowRunId: normalizeText(item.payload?.workflow_run_id),
  };
}

function buildSeverityBreakdown(value: unknown): LabeledValue[] {
  const record = isRecord(value) ? value : {};
  return ["critical", "high", "medium", "low", "unknown"].map((severity) => ({
    label: formatSeverityLabel(severity),
    value: normalizeNumber(record[severity]),
    display: String(normalizeNumber(record[severity])),
    tone: severityMeta[severity]?.tone ?? "muted",
  }));
}

function weightsToList(value: unknown): LabeledValue[] {
  if (!isRecord(value)) {
    return [];
  }
  return Object.entries(value)
    .map(([label, raw]) => ({
      label,
      value: normalizeNumber(raw),
      display: formatPercent(raw),
      tone: "blue" as Tone,
    }))
    .sort((a, b) => b.value - a.value);
}

function formatModelSource(value: unknown): string {
  const record = isRecord(value) ? value : {};
  const model = normalizeNumber(record.model);
  const rule = normalizeNumber(record.rule);
  if (!model && !rule) {
    return "暂无来源统计";
  }
  return `模型 ${model} / 规则 ${rule}`;
}

function formatDuration(startedAt: unknown, finishedAt: unknown): string {
  const start = Date.parse(normalizeText(startedAt));
  const finish = Date.parse(normalizeText(finishedAt));
  if (!Number.isFinite(start) || !Number.isFinite(finish) || finish < start) {
    return "-";
  }
  const seconds = Math.round((finish - start) / 1000);
  if (seconds < 120) {
    return `${seconds} 秒`;
  }
  return `${Math.round(seconds / 60)} 分钟`;
}

function formatPercent(value: unknown): string {
  const numeric = normalizeNumber(value);
  return `${Math.round(numeric * 100)}%`;
}

function formatScore(value: unknown): string {
  const numeric = normalizeNumber(value);
  return numeric ? numeric.toFixed(2).replace(/\.00$/, "") : "-";
}

function asArray(value: unknown): any[] {
  return Array.isArray(value) ? value : [];
}

function normalizeNumber(value: unknown): number {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
}

function normalizeText(value: unknown): string {
  return value == null ? "" : String(value).trim();
}

function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isPresent<T>(value: T | null | undefined | false): value is T {
  return Boolean(value);
}
