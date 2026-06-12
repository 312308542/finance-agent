import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Buffer } from "node:buffer";
import ts from "typescript";

const source = await readFile(new URL("../src/consolePagesView.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const {
  buildAgentPageModel,
  buildMemoryPageModel,
  buildPortfolioPageModel,
  buildRiskPageModel,
  formatSeverityLabel,
} = await import(moduleUrl);

assert.equal(formatSeverityLabel("critical"), "严重");
assert.equal(formatSeverityLabel("high"), "高");
assert.equal(formatSeverityLabel("medium"), "中");

const portfolio = {
  status: "ok",
  active_portfolio_id: "portfolio:demo",
  portfolios: [{ portfolio_id: "portfolio:demo", name: "测试组合", total_equity: "100000" }],
  positions: [
    {
      asset_id: "ashare:600519",
      symbol: "600519",
      market: "ashare",
      side: "long",
      market_value: "30000",
      unrealized_pnl: "20000",
      unrealized_pnl_pct: "0.20",
      portfolio_weight: "0.30",
      payload: { industry: "白酒" },
    },
  ],
  concentration_warnings: [
    {
      asset_id: "ashare:600519",
      symbol: "600519",
      weight: "0.30",
      threshold: "0.25",
      message: "单标的持仓权重超过组合阈值。",
    },
  ],
  metrics: {
    position_count: 1,
    positive_position_count: 1,
    negative_position_count: 0,
    risk_profile: "balanced",
    concentration: {
      max_position_weight: "0.30",
      max_position_asset_id: "ashare:600519",
      market_weights: { ashare: "0.30" },
      industry_weights: { 白酒: "0.30" },
      over_position_threshold_count: 1,
    },
  },
};

const portfolioModel = buildPortfolioPageModel(portfolio);
assert.equal(portfolioModel.metrics.positionCount, 1);
assert.equal(portfolioModel.metrics.maxPositionWeightDisplay, "30%");
assert.equal(portfolioModel.positions[0].weightDisplay, "30%");
assert.equal(portfolioModel.concentrationWarnings[0].symbol, "600519");
assert.equal(portfolioModel.industryWeights[0].label, "白酒");

const risk = {
  status: "ok",
  alerts: [{ alert_id: "alert:1", alert_type: "drawdown", severity: "high", status: "open", as_of: "2026-06-12T10:00:00+08:00", payload: { summary: "回撤过大" } }],
  triggers: [{ trigger_event_id: "trigger:1", trigger_type: "intraday_drop", severity: "medium", status: "pending", triggered_at: "2026-06-12T10:01:00+08:00", payload: { reason: "盘中急跌" } }],
  risk_findings: [{ risk_id: "risk:1", risk_type: "trend_break", title: "趋势破位", severity: "critical", as_of: "2026-06-12T10:02:00+08:00" }],
  data_quality: [{ quality_id: "quality:1", market: "ashare", data_domain: "market_bars", status: "stale", issue_count: 3 }],
  metrics: {
    alert_count: 1,
    trigger_count: 1,
    risk_finding_count: 1,
    high_severity_count: 2,
    risk_severity_breakdown: { critical: 1, high: 0, medium: 0, low: 0, unknown: 0 },
  },
};

const riskModel = buildRiskPageModel(risk);
assert.equal(riskModel.metrics.highSeverityCount, 2);
assert.equal(riskModel.severityBreakdown[0].label, "严重");
assert.equal(riskModel.events.length, 2);
assert.equal(riskModel.findings[0].title, "趋势破位");

const workflows = {
  status: "ok",
  available: [{ workflow_type: "portfolio_monitoring" }],
  runs: [
    {
      workflow_run_id: "workflow:1",
      workflow_type: "portfolio_monitoring",
      trigger_type: "manual",
      trigger_ref: "portfolio:demo",
      status: "succeeded",
      started_at: "2026-06-12T09:00:00+08:00",
      finished_at: "2026-06-12T09:01:30+08:00",
      payload: {
        model_roundtable: { generated_by: { model: 2, rule: 1 } },
        review_status: "approved_by_review",
      },
    },
  ],
  metrics: { recent_count: 1, running_count: 0, failed_count: 0 },
};

const agentModel = buildAgentPageModel(workflows);
assert.equal(agentModel.metrics.availableCount, 1);
assert.equal(agentModel.runs[0].durationDisplay, "90 秒");
assert.equal(agentModel.runs[0].modelSourceDisplay, "模型 2 / 规则 1");

const memories = {
  status: "ok",
  items: [
    {
      memory_id: "memory:1",
      memory_type: "decision_summary",
      scope: "asset",
      asset_id: "ashare:600519",
      content: "用户确认继续观察。",
      confidence: "0.8",
      status: "active",
      created_at: "2026-06-12T10:00:00+08:00",
      payload: { workflow_run_id: "workflow:1" },
    },
  ],
  metrics: { memory_count: 1, stale_memory_count: 0, asset_count: 1 },
};

const memoryModel = buildMemoryPageModel(memories);
assert.equal(memoryModel.metrics.memoryCount, 1);
assert.equal(memoryModel.items[0].confidenceDisplay, "80%");
assert.equal(memoryModel.items[0].typeLabel, "决策摘要");
