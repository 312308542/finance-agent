export type PositionMonitoringItem = {
  positionId: string;
  assetId: string;
  assetLabel: string;
  action: string;
  intendedAction: string;
  executionStatus: string;
  severity: string;
  sellableQuantity: number;
  plannedHorizonDays: number;
  structureDirection: string;
  protectivePrice: number | null;
  sectorRegime: string;
  reasonCodes: string[];
  lastEvaluatedAt: string;
  nextReviewAt: string;
};

const actionLabels: Record<string, string> = {
  hold: "持有",
  add_blocked: "禁止加仓",
  watch: "观察",
  reduce: "减仓建议",
  exit: "退出建议",
  unexecutable: "暂不可执行",
};

export function buildPositionMonitorModel(payload: Record<string, any> | null | undefined) {
  const rows = Array.isArray(payload?.positions) ? payload.positions : [];
  const items = rows.map(normalizePositionMonitoringItem).filter((item): item is PositionMonitoringItem => Boolean(item));
  const severityOrder: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };
  items.sort((a, b) => (severityOrder[a.severity] ?? 9) - (severityOrder[b.severity] ?? 9));
  return {
    status: String(payload?.status || "empty"),
    items,
    urgentCount: items.filter((item) => item.severity === "critical" || item.severity === "high").length,
    emptyText: "暂无持仓监控数据。登记执行结果后，系统会在最新行情到达时建立监控状态。",
  };
}

export function monitoringActionLabel(action: string): string {
  return actionLabels[action] || action || "观察";
}

function normalizePositionMonitoringItem(item: unknown): PositionMonitoringItem | null {
  if (!item || typeof item !== "object") {
    return null;
  }
  const row = item as Record<string, any>;
  const monitoring = row.monitoring && typeof row.monitoring === "object" ? row.monitoring : {};
  const symbol = String(row.symbol || row.asset_id || "");
  const name = String(row.name || row.payload?.name || "");
  return {
    positionId: String(row.position_id || ""),
    assetId: String(row.asset_id || ""),
    assetLabel: `${symbol}${name ? ` ${name}` : ""}`.trim(),
    action: String(monitoring.action || "watch"),
    intendedAction: String(monitoring.intended_action || monitoring.action || "watch"),
    executionStatus: String(monitoring.execution_status || "not_executed"),
    severity: String(monitoring.severity || "low"),
    sellableQuantity: Number(monitoring.sellable_quantity || 0),
    plannedHorizonDays: Number(monitoring.planned_horizon_days || 10),
    structureDirection: String(monitoring.structure_direction || "unknown"),
    protectivePrice: monitoring.protective_price === null || monitoring.protective_price === undefined
      ? null
      : Number(monitoring.protective_price),
    sectorRegime: String(monitoring.sector_regime || "unknown"),
    reasonCodes: Array.isArray(monitoring.reason_codes) ? monitoring.reason_codes.map(String) : [],
    lastEvaluatedAt: String(monitoring.last_evaluated_at || ""),
    nextReviewAt: String(monitoring.next_review_at || ""),
  };
}
