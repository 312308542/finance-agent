export type ApiStatus = "ok" | "empty" | "partial" | "unavailable" | string;

export type DashboardSummary = {
  owner_id: string;
  status: ApiStatus;
  source?: "api" | "fallback";
  generated_at?: string;
  message?: string;
  sections: {
    portfolio: Record<string, any>;
    watchlists: Record<string, any>;
    recommendations: Record<string, any>;
    risks: Record<string, any>;
    workflows: Record<string, any>;
    memories: Record<string, any>;
    data_health: Record<string, any>;
    models: Record<string, any>;
  };
};

export type ModelProviderPayload = {
  provider_vendor: string;
  provider_name: string;
  base_url?: string | null;
  api_key?: string | null;
  timeout_seconds: number;
  is_enabled: boolean;
  is_default?: boolean;
};

export type ModelProviderConnectivityPayload = {
  provider_key: string;
  model_key: string;
  model_name: string;
  base_url: string | null;
  api_key: string | null;
  timeout_seconds: number;
};

export type ModelProviderConnectivityInput = {
  providerKey: string;
  modelKey: string;
  modelName: string;
  baseUrl: string;
  apiKey: string;
  apiKeyIsPreview: boolean;
};

export type ModelInstancePayload = {
  provider_key: string;
  model_name: string;
  model_type?: string;
  role?: string | null;
  route_priority?: number;
  timeout_seconds?: number;
  is_enabled: boolean;
  is_default?: boolean;
};

export type ModelRoutePayload = {
  workflow_type: string;
  task: string;
  model_key: string;
  decision_type?: string;
  reason?: string | null;
  priority?: number;
  is_enabled: boolean;
};

export type DataSyncConfigPayload = {
  preset: string;
  markets: string[];
  enabled: boolean;
  cache_backend: string;
  max_concurrent_jobs: number;
};

export type DataSchedulerStartPayload = {
  dry_run: boolean;
  max_cycles?: number | null;
};

export type DataSchedulerJobUpdatePayload = {
  enabled?: boolean | null;
  interval_seconds?: number | null;
  limit?: number | null;
  batch_size?: number | null;
  max_workers?: number | null;
  schedule_type?: string | null;
  run_at?: string[] | null;
  timezone?: string | null;
  trading_day_policy?: string | null;
};

export type DataSchedulerJobRunPayload = {
  dry_run: boolean;
};

export type DecisionConfirmationPayload = {
  feedback: "accepted" | "rejected" | "modified" | "deferred";
  comment?: string;
  modified_action?: string;
};

export type ExecutionRecordPayload = {
  owner_id: string;
  portfolio_id: string;
  asset_id: string;
  market: string;
  action: string;
  executed_price: string;
  executed_quantity: string;
  executed_at: string;
  execution_id?: string;
  order_draft_id?: string;
  decision_log_id?: string;
  fee?: string;
  note?: string;
  source: "user_reported";
};

export type ChatStreamEvent = {
  event: string;
  data: Record<string, any>;
};

const apiBase = (import.meta.env.VITE_FINANCE_AGENT_API_BASE || "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);
const requestTimeoutMs = 8000;

export async function loadDashboardSummary(ownerId: string): Promise<DashboardSummary> {
  try {
    const response = await fetchWithTimeout(
      `${apiBase}/api/dashboard/summary?owner_id=${ownerId}`,
      { timeoutMs: requestTimeoutMs },
    );
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = (await response.json()) as DashboardSummary;
    return { ...payload, source: "api" };
  } catch (error) {
    return {
      ...fallbackSummary,
      owner_id: ownerId,
      source: "fallback",
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export async function postChatMessage(ownerId: string, message: string): Promise<Record<string, any>> {
  try {
    const response = await fetchWithTimeout(`${apiBase}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner_id: ownerId, message }),
      timeoutMs: requestTimeoutMs,
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  } catch (error) {
    return {
      status: "unavailable",
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export function parseChatStreamChunk(chunk: string, previous = ""): { events: ChatStreamEvent[]; remainder: string } {
  const text = `${previous}${chunk}`;
  const frames = text.split("\n\n");
  const remainder = frames.pop() ?? "";
  const events = frames
    .map((frame) => {
      const lines = frame.split(/\r?\n/);
      const eventLine = lines.find((line) => line.startsWith("event:"));
      const dataLines = lines.filter((line) => line.startsWith("data:"));
      if (!eventLine || !dataLines.length) {
        return null;
      }
      try {
        return {
          event: eventLine.replace(/^event:\s*/, "").trim(),
          data: JSON.parse(dataLines.map((line) => line.replace(/^data:\s*/, "")).join("\n")),
        };
      } catch {
        return null;
      }
    })
    .filter((event): event is ChatStreamEvent => Boolean(event));
  return { events, remainder };
}

export async function streamChatMessage(
  ownerId: string,
  message: string,
  handlers: {
    onEvent: (event: ChatStreamEvent) => void;
    sessionId?: string | null;
  },
): Promise<void> {
  const response = await fetch(`${apiBase}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      owner_id: ownerId,
      message,
      session_id: handlers.sessionId || null,
    }),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  if (!response.body) {
    throw new Error("浏览器不支持流式响应读取");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let remainder = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    const parsed = parseChatStreamChunk(decoder.decode(value, { stream: true }), remainder);
    remainder = parsed.remainder;
    parsed.events.forEach(handlers.onEvent);
  }
  const tail = decoder.decode();
  if (tail || remainder) {
    parseChatStreamChunk(tail, remainder).events.forEach(handlers.onEvent);
  }
}

export async function saveModelProvider(
  providerKey: string,
  payload: ModelProviderPayload,
): Promise<Record<string, any>> {
  return sendJson(`/api/models/providers/${encodeURIComponent(providerKey)}`, payload);
}

export async function revealModelProviderSecret(providerKey: string): Promise<Record<string, any>> {
  return getJson(`/api/models/providers/${encodeURIComponent(providerKey)}/secret`, {
    api_key: null,
    api_key_configured: false,
  });
}

export function buildModelProviderConnectivityPayload(
  input: ModelProviderConnectivityInput,
): ModelProviderConnectivityPayload {
  const modelKey = input.modelKey.trim();
  return {
    provider_key: input.providerKey,
    model_key: modelKey,
    model_name: input.modelName.trim() || modelKey,
    base_url: input.baseUrl.trim() || null,
    api_key: input.apiKeyIsPreview ? null : input.apiKey.trim() || null,
    timeout_seconds: 30,
  };
}

export async function testModelProviderConnectivity(
  payload: ModelProviderConnectivityPayload,
): Promise<Record<string, any>> {
  return requestJson("POST", "/api/models/providers/test-connectivity", payload, 45000);
}

export async function saveModelInstance(
  modelKey: string,
  payload: ModelInstancePayload,
): Promise<Record<string, any>> {
  return sendJson(`/api/models/instances/${encodeURIComponent(modelKey)}`, payload);
}

export async function deleteModelInstance(modelKey: string): Promise<Record<string, any>> {
  return deleteJson(`/api/models/instances/${encodeURIComponent(modelKey)}`);
}

export async function saveModelRoute(
  role: string,
  payload: ModelRoutePayload,
): Promise<Record<string, any>> {
  return sendJson(`/api/models/routes/${encodeURIComponent(role)}`, payload);
}

export async function loadModelRoutePreview(): Promise<Record<string, any>> {
  try {
    const response = await fetchWithTimeout(
      `${apiBase}/api/models/routes/preview?workflow_type=portfolio_monitoring&task=agent_loop_planning&high_risk=true`,
      { timeoutMs: requestTimeoutMs },
    );
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  } catch (error) {
    return {
      status: "unavailable",
      message: error instanceof Error ? error.message : String(error),
      data: { routes: [] },
    };
  }
}

export async function loadReports(ownerId: string, limit = 30): Promise<Record<string, any>> {
  const normalizedLimit = Math.min(200, Math.max(1, Math.round(limit)));
  return getJson(
    `/api/reports?owner_id=${encodeURIComponent(ownerId)}&limit=${normalizedLimit}`,
    { items: [], metrics: {} },
  );
}

export async function loadReportDetail(workflowRunId: string): Promise<Record<string, any>> {
  if (!workflowRunId.trim()) {
    return { status: "empty", data: {} };
  }
  return getJson(`/api/reports/${encodeURIComponent(workflowRunId)}`, {
    workflow_run_id: workflowRunId,
    report: null,
    review_results: [],
    report_review_appended: null,
  });
}

export async function loadPortfolioOverview(ownerId: string): Promise<Record<string, any>> {
  return getJson(`/api/portfolio/overview?owner_id=${encodeURIComponent(ownerId)}`, {
    status: "empty",
    portfolios: [],
    positions: [],
    concentration_warnings: [],
    metrics: {},
  });
}

export async function loadRiskOverview(ownerId: string, limit = 50): Promise<Record<string, any>> {
  const normalizedLimit = Math.min(200, Math.max(1, Math.round(limit)));
  return getJson(
    `/api/risks?owner_id=${encodeURIComponent(ownerId)}&limit=${normalizedLimit}`,
    {
      status: "empty",
      triggers: [],
      alerts: [],
      risk_findings: [],
      data_quality: [],
      metrics: {},
    },
  );
}

export async function loadWorkflowOverview(ownerId: string, limit = 50): Promise<Record<string, any>> {
  const normalizedLimit = Math.min(200, Math.max(1, Math.round(limit)));
  const payload = await getJson(
    `/api/dashboard/summary?owner_id=${encodeURIComponent(ownerId)}&limit=${normalizedLimit}`,
    {
      sections: {
        workflows: { status: "empty", runs: [], available: [], metrics: {} },
      },
    },
  );
  return payload.sections?.workflows ?? { status: "empty", runs: [], available: [], metrics: {} };
}

export async function loadRecentMemories(ownerId: string, limit = 50): Promise<Record<string, any>> {
  const normalizedLimit = Math.min(200, Math.max(1, Math.round(limit)));
  return getJson(
    `/api/memories/recent?owner_id=${encodeURIComponent(ownerId)}&limit=${normalizedLimit}`,
    { status: "empty", items: [], metrics: {} },
  );
}

export async function loadLatestRecommendations(
  ownerId: string,
  market?: string | null,
  limit = 50,
): Promise<Record<string, any>> {
  const normalizedLimit = Math.min(200, Math.max(1, Math.round(limit)));
  const params = new URLSearchParams({
    owner_id: ownerId,
    limit: String(normalizedLimit),
  });
  if (market) {
    params.set("market", market);
  }
  return getJson(`/api/recommendations/latest?${params.toString()}`, {
    runs: [],
    recommendations: [],
    metrics: {},
  });
}

export async function loadPendingDecisions(ownerId: string, limit = 50): Promise<Record<string, any>> {
  const normalizedLimit = Math.min(200, Math.max(1, Math.round(limit)));
  return getJson(
    `/api/decisions/pending-confirmation?owner_id=${encodeURIComponent(ownerId)}&limit=${normalizedLimit}`,
    { items: [] },
  );
}

export async function submitDecisionFeedback(
  decisionId: string,
  payload: Record<string, unknown>,
): Promise<Record<string, any>> {
  return requestJson(
    "POST",
    `/api/decisions/${encodeURIComponent(decisionId)}/feedback`,
    payload,
    requestTimeoutMs,
  );
}

export async function confirmDecision(
  decisionId: string,
  payload: DecisionConfirmationPayload,
): Promise<Record<string, any>> {
  return requestJson(
    "POST",
    `/api/decisions/${encodeURIComponent(decisionId)}/confirm`,
    payload,
    requestTimeoutMs,
  );
}

export async function createOrderDraft(decisionId: string): Promise<Record<string, any>> {
  return requestJson(
    "POST",
    `/api/decisions/${encodeURIComponent(decisionId)}/order-draft`,
    {},
    requestTimeoutMs,
  );
}

export async function loadOrderDrafts(
  ownerId: string,
  status?: string | null,
  limit = 50,
): Promise<Record<string, any>> {
  const params = new URLSearchParams({
    owner_id: ownerId,
    limit: String(Math.min(200, Math.max(1, Math.round(limit)))),
  });
  if (status) {
    params.set("status", status);
  }
  return getJson(`/api/order-drafts?${params.toString()}`, { items: [] });
}

export async function recordExecution(payload: ExecutionRecordPayload): Promise<Record<string, any>> {
  return requestJson("POST", "/api/executions", payload, requestTimeoutMs);
}

export async function loadExecutionRecords(
  ownerId: string,
  assetId?: string | null,
  limit = 50,
): Promise<Record<string, any>> {
  const params = new URLSearchParams({
    owner_id: ownerId,
    limit: String(Math.min(200, Math.max(1, Math.round(limit)))),
  });
  if (assetId) {
    params.set("asset_id", assetId);
  }
  return getJson(`/api/executions?${params.toString()}`, { items: [] });
}

export async function loadUpcomingReviews(ownerId: string, limit = 50): Promise<Record<string, any>> {
  const normalizedLimit = Math.min(200, Math.max(1, Math.round(limit)));
  return getJson(
    `/api/reviews/upcoming?owner_id=${encodeURIComponent(ownerId)}&limit=${normalizedLimit}`,
    { items: [] },
  );
}

export async function loadDataSyncConfig(): Promise<Record<string, any>> {
  return getJson("/api/data/sync/config", { data: { preview: { tasks: [] }, validation: {} } });
}

export async function saveDataSyncConfig(payload: DataSyncConfigPayload): Promise<Record<string, any>> {
  return sendJson("/api/data/sync/config", payload);
}

export async function loadDataSchedulerStatus(): Promise<Record<string, any>> {
  return getJson("/api/data/scheduler/status", { health: { status: "missing" }, process: { running: false } });
}

export async function loadDataSchedulerProgress(eventLimit = 120): Promise<Record<string, any>> {
  const normalizedLimit = Math.min(200, Math.max(1, Math.round(eventLimit)));
  return getJson(`/api/data/scheduler/progress?event_limit=${normalizedLimit}`, {
    cache_backend: "null",
    tasks: [],
    waiting: [],
    metrics: {},
  });
}

export async function loadDataSchedulerJobs(): Promise<Record<string, any>> {
  return getJson("/api/data/scheduler/jobs", {
    config: {},
    jobs: [],
  });
}

export async function updateDataSchedulerJob(
  jobName: string,
  payload: DataSchedulerJobUpdatePayload,
): Promise<Record<string, any>> {
  return sendJson(`/api/data/scheduler/jobs/${encodeURIComponent(jobName)}`, payload);
}

export async function runDataSchedulerJob(
  jobName: string,
  payload: DataSchedulerJobRunPayload,
): Promise<Record<string, any>> {
  return requestJson(
    "POST",
    `/api/data/scheduler/jobs/${encodeURIComponent(jobName)}/run`,
    payload,
    45000,
  );
}

export async function rerunFailedDataSchedulerJob(
  jobName: string,
  payload: DataSchedulerJobRunPayload,
): Promise<Record<string, any>> {
  return requestJson(
    "POST",
    `/api/data/scheduler/jobs/${encodeURIComponent(jobName)}/rerun-failed`,
    payload,
    45000,
  );
}

export async function cancelDataSchedulerJob(jobName: string): Promise<Record<string, any>> {
  return requestJson(
    "POST",
    `/api/data/scheduler/jobs/${encodeURIComponent(jobName)}/cancel`,
    {},
    45000,
  );
}

export async function pauseDataSchedulerJob(jobName: string): Promise<Record<string, any>> {
  return requestJson(
    "POST",
    `/api/data/scheduler/jobs/${encodeURIComponent(jobName)}/pause`,
    {},
    45000,
  );
}

export async function resumeDataSchedulerJob(jobName: string): Promise<Record<string, any>> {
  return requestJson(
    "POST",
    `/api/data/scheduler/jobs/${encodeURIComponent(jobName)}/resume`,
    {},
    45000,
  );
}

export async function startDataScheduler(payload: DataSchedulerStartPayload): Promise<Record<string, any>> {
  return postJson("/api/data/scheduler/start", payload);
}

export async function stopDataScheduler(): Promise<Record<string, any>> {
  return postJson("/api/data/scheduler/stop", {});
}

async function getJson(path: string, fallbackData: Record<string, any>): Promise<Record<string, any>> {
  try {
    const response = await fetchWithTimeout(`${apiBase}${path}`, {
      timeoutMs: requestTimeoutMs,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data?.message || `HTTP ${response.status}`);
    }
    return data;
  } catch (error) {
    return {
      status: "unavailable",
      message: error instanceof Error ? error.message : String(error),
      data: fallbackData,
    };
  }
}

async function sendJson(path: string, payload: Record<string, unknown>): Promise<Record<string, any>> {
  return requestJson("PUT", path, payload);
}

async function postJson(path: string, payload: Record<string, unknown>): Promise<Record<string, any>> {
  return requestJson("POST", path, payload);
}

async function deleteJson(path: string): Promise<Record<string, any>> {
  return requestJson("DELETE", path, {});
}

async function requestJson(
  method: "DELETE" | "POST" | "PUT",
  path: string,
  payload: Record<string, unknown>,
  timeoutMs = requestTimeoutMs,
): Promise<Record<string, any>> {
  try {
    const response = await fetchWithTimeout(`${apiBase}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: method === "DELETE" ? undefined : JSON.stringify(payload),
      timeoutMs,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data?.message || `HTTP ${response.status}`);
    }
    return data;
  } catch (error) {
    return {
      status: "unavailable",
      message: error instanceof Error ? error.message : String(error),
      data: {},
    };
  }
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit & { timeoutMs?: number } = {},
): Promise<Response> {
  const { timeoutMs, ...requestInit } = init;
  if (!timeoutMs) {
    return fetch(url, requestInit);
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...requestInit, signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

export function statusText(status: string) {
  const map: Record<string, string> = {
    ok: "正常",
    empty: "无数据",
    partial: "部分可用",
    unavailable: "不可用",
    stale: "过期",
  };
  return map[status] ?? status;
}

export function toneByStatus(status: string) {
  if (status === "ok") return "green";
  if (status === "empty") return "amber";
  if (status === "partial" || status === "stale") return "blue";
  return "red";
}

const fallbackSummary: DashboardSummary = {
  owner_id: "fallback",
  status: "partial",
  source: "fallback",
  generated_at: new Date().toISOString(),
  sections: {
    portfolio: {
      status: "partial",
      positions: [
        {
          symbol: "000001",
          market: "ashare",
          side: "long",
          market_value: "82000",
          unrealized_pnl: "-2400",
          portfolio_weight: "0.18",
        },
        {
          symbol: "BTCUSDT",
          market: "crypto_spot",
          side: "long",
          market_value: "126000",
          unrealized_pnl: "6800",
          portfolio_weight: "0.27",
        },
      ],
      metrics: {
        position_count: 2,
        positive_position_count: 1,
        negative_position_count: 1,
        risk_profile: "平衡成长",
      },
    },
    watchlists: {
      status: "partial",
      items: [
        {
          symbol: "300750",
          market: "ashare",
          risk_level: "medium",
          source_type: "recommendation_run",
          pool: "system_research_pool",
          pool_label: "系统研究跟踪",
          reason: "资金流和行业景气度改善，等待放量确认。",
        },
        {
          symbol: "ETHUSDT",
          market: "crypto_spot",
          risk_level: "high",
          source_type: "signal_trigger",
          pool: "manual_watchlist",
          pool_label: "用户观察池",
          reason: "链上与衍生品拥挤度上升，适合持续观察而非追入。",
        },
      ],
      pools: [
        {
          key: "system_research_pool",
          label: "系统研究跟踪",
          count: 1,
          description: "系统推荐后自动跟踪，尚未代表用户确认关注。",
        },
        {
          key: "manual_watchlist",
          label: "用户观察池",
          count: 1,
          description: "用户手动加入或确认关注的资产。",
        },
        {
          key: "other_watchlist",
          label: "其他观察项",
          count: 0,
          description: "暂未归类到研究池或用户观察池的有效条目。",
        },
      ],
      metrics: { active_count: 2, high_risk_count: 1, research_count: 1, manual_count: 1 },
    },
    recommendations: {
      status: "partial",
      recommendations: [
        {
          rank: 1,
          symbol: "600519",
          name: "贵州茅台",
          market: "ashare",
          action: "watch",
          total_score: "86.4",
          confidence: "0.74",
        },
        {
          rank: 2,
          symbol: "BTCUSDT",
          name: "Bitcoin",
          market: "crypto_spot",
          action: "wait_for_pullback",
          total_score: "82.1",
          confidence: "0.69",
        },
      ],
      metrics: { recommendation_count: 2, buy_count: 0, watch_count: 1 },
    },
    risks: {
      status: "partial",
      triggers: [
        {
          trigger_type: "position_drawdown",
          severity: "high",
          requested_workflow_type: "portfolio_monitoring",
          payload: { reason: "单标的回撤超过阈值，需要复核是否减仓。" },
        },
        {
          trigger_type: "data_quality_degraded",
          severity: "medium",
          requested_workflow_type: "daily_review",
          payload: { reason: "部分资金流数据过期，推荐置信度下降。" },
        },
      ],
      alerts: [],
      data_quality: [],
      metrics: { high_severity_count: 1, trigger_count: 2, alert_count: 0 },
    },
    workflows: {
      status: "partial",
      available: [
        { workflow_type: "portfolio_monitoring" },
        { workflow_type: "watchlist_management" },
        { workflow_type: "recommendation_decision" },
      ],
      runs: [
        {
          workflow_type: "portfolio_monitoring",
          status: "completed",
          started_at: "2026-05-22T09:30:00+08:00",
        },
        {
          workflow_type: "watchlist_management",
          status: "running",
          started_at: "2026-05-22T09:35:00+08:00",
        },
      ],
      metrics: { recent_count: 2, failed_count: 0 },
    },
    memories: {
      status: "partial",
      memories: [
        {
          memory_type: "candidate_intake_reason",
          status: "active",
          content: "300750 入池原因：行业资金回流，但需要等待量能确认。",
        },
        {
          memory_type: "review_result",
          status: "active",
          content: "上次追高数字货币回撤较大，后续优先等待回调。",
        },
      ],
      decisions: [],
      metrics: { memory_count: 2, decision_count: 0 },
    },
    data_health: {
      status: "partial",
      items: [
        {
          market: "ashare",
          data_domain: "market_bars",
          provider: "akshare",
          status: "partial",
          issue_count: 1,
        },
        {
          market: "crypto_spot",
          data_domain: "ohlcv",
          provider: "binance",
          status: "ok",
          issue_count: 0,
        },
      ],
      metrics: { quality_count: 2, issue_count: 1 },
    },
    models: {
      status: "partial",
      models: [
        { model_key: "deepseek-v4-pro", role: "primary_financial_analyst" },
        { model_key: "gpt-5.5-pro", role: "high_risk_reviewer" },
      ],
    },
  },
};
