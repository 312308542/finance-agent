export type ReportTone = "green" | "amber" | "blue" | "red" | "muted";

export type ReportStatusMeta = {
  label: string;
  tone: ReportTone;
};

export type ReportListItem = {
  workflowRunId: string;
  workflowType: string;
  workflowLabel: string;
  title: string;
  summary: string;
  status: string;
  statusLabel: string;
  statusTone: ReportTone;
  reviewStatus: string;
  reviewLabel: string;
  reviewTone: ReportTone;
  createdAt: string;
  recommendationCount: number;
  riskCount: number;
  payload: Record<string, any>;
};

export type ReportDetailModel = {
  workflowRunId: string;
  workflowType: string;
  markdown: string;
  html: string;
  reviewResults: Array<Record<string, any>>;
  reviewAppended: boolean;
  anchors: Array<{ id: string; label: string }>;
  raw: Record<string, any>;
};

export type ReportPageModel = {
  status: string;
  message: string;
  items: ReportListItem[];
  selected: ReportListItem | null;
  detail: ReportDetailModel | null;
  metrics: {
    totalCount: number;
    reviewedCount: number;
    pendingReviewCount: number;
    rejectedCount: number;
  };
  emptyText: string;
};

const workflowLabels: Record<string, string> = {
  asset_deep_analysis: "单标的深度分析",
  portfolio_monitoring: "持仓监控",
  watchlist_management: "观察池管理",
  recommendation_decision: "推荐决策",
  swap_decision: "换股/换币比较",
  daily_review: "每日复盘",
  balanced_swing_v1: "均衡波段策略",
  risk_review: "风险复核",
};

const anchorLabels = ["结论", "证据", "风险反驳", "行动", "确认边界"];

export function formatReportStatus(status: string | null | undefined): ReportStatusMeta {
  const normalized = normalizeText(status).toLowerCase();
  if (["succeeded", "success", "completed", "ok", "approved"].includes(normalized)) {
    return { label: "已完成", tone: "green" };
  }
  if (["running", "processing", "in_progress", "pending"].includes(normalized)) {
    return { label: "生成中", tone: "blue" };
  }
  if (["failed", "error", "cancelled", "canceled"].includes(normalized)) {
    return { label: "失败", tone: "red" };
  }
  if (!normalized || normalized === "unknown") {
    return { label: "未知", tone: "muted" };
  }
  return { label: normalized, tone: "amber" };
}

export function formatReviewStatus(status: string | null | undefined): ReportStatusMeta {
  const normalized = normalizeText(status).toLowerCase();
  if (["approved", "approve", "approved_by_review", "passed", "pass"].includes(normalized)) {
    return { label: "复核通过", tone: "green" };
  }
  if (["rejected", "reject", "blocked", "failed"].includes(normalized)) {
    return { label: "复核驳回", tone: "red" };
  }
  if (["pending_manual", "manual_required", "requires_manual_review"].includes(normalized)) {
    return { label: "待人工复核", tone: "amber" };
  }
  if (["requires_model_review", "pending_model", "reviewing", "pending"].includes(normalized)) {
    return { label: "待模型复核", tone: "blue" };
  }
  if (!normalized || normalized === "unknown") {
    return { label: "未复核", tone: "muted" };
  }
  return { label: normalized, tone: "amber" };
}

export function buildReportPageModel(
  listPayload: Record<string, any> | null | undefined,
  detailPayload: Record<string, any> | null | undefined,
  selectedWorkflowRunId: string | null | undefined,
): ReportPageModel {
  const items = normalizeReportItems(listPayload);
  const selected =
    items.find((item) => item.workflowRunId === selectedWorkflowRunId) ?? items[0] ?? null;
  return {
    status: normalizeText(listPayload?.status) || "empty",
    message: normalizeText(listPayload?.message),
    items,
    selected,
    detail: selected ? normalizeReportDetail(detailPayload, selected.workflowRunId) : null,
    metrics: normalizeReportMetrics(listPayload?.metrics, items),
    emptyText: "暂无报告，等待 Workflow 生成中文报告。",
  };
}

export function normalizeReportItems(payload: Record<string, any> | null | undefined): ReportListItem[] {
  const rows = Array.isArray(payload?.items) ? payload.items : [];
  return rows
    .map((item) => normalizeReportItem(item))
    .filter((item): item is ReportListItem => Boolean(item));
}

export function normalizeReportDetail(
  payload: Record<string, any> | null | undefined,
  fallbackWorkflowRunId: string,
): ReportDetailModel | null {
  const data = isRecord(payload?.data) ? payload.data : payload;
  if (!isRecord(data)) {
    return null;
  }
  const report = isRecord(data.report) ? data.report : {};
  const markdown = normalizeText(data.markdown || report.markdown || report.report || data.report);
  const workflowRunId = normalizeText(data.workflow_run_id || fallbackWorkflowRunId);
  const workflowType = normalizeText(report.workflow_type || data.workflow_type);
  const reviewResults = Array.isArray(data.review_results)
    ? data.review_results.filter(isRecord)
    : Array.isArray(report.review_results)
      ? report.review_results.filter(isRecord)
      : [];
  return {
    workflowRunId,
    workflowType,
    markdown,
    html: renderTrustedReportMarkdown(markdown),
    reviewResults,
    reviewAppended: Boolean(data.report_review_appended || report.report_review_appended),
    anchors: anchorLabels.map((label) => ({ id: slugify(label), label })),
    raw: data,
  };
}

export function renderTrustedReportMarkdown(markdown: string | null | undefined): string {
  const lines = normalizeText(markdown).split(/\r?\n/);
  const html: string[] = [];
  let listItems: string[] = [];
  let quoteLines: string[] = [];
  let tableRows: string[][] = [];

  const closeList = () => {
    if (!listItems.length) {
      return;
    }
    html.push(`<ul>${listItems.map((item) => `<li>${formatInline(item)}</li>`).join("")}</ul>`);
    listItems = [];
  };
  const closeQuote = () => {
    if (!quoteLines.length) {
      return;
    }
    html.push(`<blockquote>${quoteLines.map((line) => `<p>${formatInline(line)}</p>`).join("")}</blockquote>`);
    quoteLines = [];
  };
  const closeTable = () => {
    if (!tableRows.length) {
      return;
    }
    const [head, separator, ...body] = tableRows;
    const hasSeparator = separator?.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
    if (!hasSeparator) {
      tableRows.forEach((row) => {
        html.push(`<p>${formatInline(row.join(" | "))}</p>`);
      });
      tableRows = [];
      return;
    }
    html.push(
      `<table><thead><tr>${head
        .map((cell) => `<th>${formatInline(cell)}</th>`)
        .join("")}</tr></thead><tbody>${body
        .map((row) => `<tr>${row.map((cell) => `<td>${formatInline(cell)}</td>`).join("")}</tr>`)
        .join("")}</tbody></table>`,
    );
    tableRows = [];
  };
  const closeBlocks = () => {
    closeList();
    closeQuote();
    closeTable();
  };

  lines.forEach((rawLine) => {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();
    if (!trimmed) {
      closeBlocks();
      return;
    }
    if (isTableLine(trimmed)) {
      closeList();
      closeQuote();
      tableRows.push(parseTableLine(trimmed));
      return;
    }
    closeTable();
    const heading = /^(#{1,4})\s+(.+)$/.exec(trimmed);
    if (heading) {
      closeList();
      closeQuote();
      const level = heading[1].length;
      const label = heading[2].trim();
      html.push(`<h${level} id="${escapeAttribute(slugify(label))}">${formatInline(label)}</h${level}>`);
      return;
    }
    const bullet = /^[-*]\s+(.+)$/.exec(trimmed);
    if (bullet) {
      closeQuote();
      listItems.push(bullet[1]);
      return;
    }
    const quote = /^>\s?(.+)$/.exec(trimmed);
    if (quote) {
      closeList();
      quoteLines.push(quote[1]);
      return;
    }
    closeList();
    closeQuote();
    html.push(`<p>${formatInline(trimmed)}</p>`);
  });
  closeBlocks();
  return html.join("");
}

function normalizeReportItem(item: unknown): ReportListItem | null {
  if (!isRecord(item)) {
    return null;
  }
  const workflowRunId = normalizeText(item.workflow_run_id || item.workflowRunId || item.run_id);
  if (!workflowRunId) {
    return null;
  }
  const workflowType = normalizeText(item.workflow_type || item.workflowType);
  const status = normalizeText(item.status);
  const reviewStatus = normalizeText(item.review_status || item.reviewStatus);
  const statusMeta = formatReportStatus(status);
  const reviewMeta = formatReviewStatus(reviewStatus);
  const workflowLabel = (workflowLabels[workflowType] ?? workflowType) || "Workflow";
  const fallbackTitle = `${workflowLabel} 报告`;
  return {
    workflowRunId,
    workflowType,
    workflowLabel,
    title: normalizeText(item.title) || fallbackTitle,
    summary: normalizeText(item.summary || item.output_ref || item.trigger_ref),
    status,
    statusLabel: statusMeta.label,
    statusTone: statusMeta.tone,
    reviewStatus,
    reviewLabel: reviewMeta.label,
    reviewTone: reviewMeta.tone,
    createdAt: normalizeText(item.created_at || item.started_at || item.finished_at),
    recommendationCount: normalizeNumber(item.recommendation_count),
    riskCount: normalizeNumber(item.risk_count),
    payload: isRecord(item.payload) ? item.payload : {},
  };
}

function normalizeReportMetrics(metrics: unknown, items: ReportListItem[]) {
  const source = isRecord(metrics) ? metrics : {};
  return {
    totalCount: normalizeNumber(source.total_count ?? source.report_count ?? items.length),
    reviewedCount: normalizeNumber(
      source.reviewed_count ?? items.filter((item) => item.reviewTone === "green").length,
    ),
    pendingReviewCount: normalizeNumber(
      source.pending_review_count ??
        items.filter((item) => item.reviewTone === "blue" || item.reviewTone === "amber").length,
    ),
    rejectedCount: normalizeNumber(
      source.rejected_count ?? items.filter((item) => item.reviewTone === "red").length,
    ),
  };
}

function formatInline(value: string): string {
  let text = escapeHtml(value);
  text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return text;
}

function isTableLine(line: string): boolean {
  return line.startsWith("|") && line.endsWith("|") && line.split("|").length > 2;
}

function parseTableLine(line: string): string[] {
  return line
    .slice(1, -1)
    .split("|")
    .map((cell) => cell.trim());
}

function slugify(value: string): string {
  return normalizeText(value).replace(/\s+/g, "-") || "section";
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttribute(value: string): string {
  return escapeHtml(value).replace(/'/g, "&#39;");
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
