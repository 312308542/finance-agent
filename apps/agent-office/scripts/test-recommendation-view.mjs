import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Buffer } from "node:buffer";
import ts from "typescript";

const source = await readFile(new URL("../src/recommendationView.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const {
  actionLabel,
  buildDecisionFeedbackPayload,
  buildRecommendationPageModel,
  formatMarketLabel,
  mergeRecommendationPayloads,
  normalizeRecommendationItem,
} = await import(moduleUrl);

assert.equal(formatMarketLabel("ashare"), "A 股");
assert.equal(formatMarketLabel("crypto_spot"), "数字货币现货");
assert.equal(actionLabel("buy"), "候选买入");
assert.equal(actionLabel("watch"), "建议观察");
assert.equal(actionLabel("wait_for_pullback"), "建议等待回撤");

const payload = {
  status: "ok",
  active_run: {
    run_id: "run:ashare:latest",
    market: "ashare",
    strategy: "balanced_swing_v1",
    horizon: "swing",
    started_at: "2026-06-12T15:20:00+08:00",
    payload: {
      avoid_pool_excluded: {
        count: 2,
        assets: [
          { asset_id: "ashare:600519", symbol: "600519", reason: "高风险回避" },
          { asset_id: "ashare:300750", symbol: "300750", reason: "数据质量不足" },
        ],
      },
    },
  },
  runs: [
    { run_id: "run:ashare:latest", market: "ashare", strategy: "balanced_swing_v1", status: "available" },
    { run_id: "run:crypto:latest", market: "crypto_spot", strategy: "crypto_momentum", status: "available" },
  ],
  recommendations: [
    {
      recommendation_id: "rec:1",
      run_id: "run:ashare:latest",
      asset_id: "ashare:000001",
      symbol: "000001",
      name: "平安银行",
      market: "ashare",
      action: "buy",
      rank: 1,
      total_score: "86.45",
      confidence: "0.74",
      risk_ids: ["risk:1", "risk:2"],
      evidence_ids: ["ev:1"],
      summary: "评分领先，但仍需确认风险。",
      payload: {
        score_breakdown: {
          technical: 31.2,
          liquidity: 18.4,
          risk: -6,
        },
        risk_rebuttal: "短期波动升高，仓位需要受限。",
        workflow_run_id: "workflow:report:1",
      },
    },
    {
      recommendation_id: "rec:2",
      run_id: "run:crypto:latest",
      asset_id: "crypto_spot:BTCUSDT",
      symbol: "BTCUSDT",
      name: "Bitcoin",
      market: "crypto_spot",
      action: "watch",
      rank: 2,
      total_score: "80.1",
      confidence: "0.61",
      risk_ids: [],
      evidence_ids: [],
      summary: "等待量能确认。",
      payload: {},
    },
  ],
  metrics: { recommendation_count: 2, buy_count: 1, watch_count: 1 },
};

const decisions = {
  status: "ok",
  data: {
    items: [
      {
        decision_id: "decision:1",
        asset_id: "ashare:000001",
        source_recommendation_id: "rec:1",
        suggested_action: "watch",
        user_action: "pending_user_confirmation",
        summary: "等待用户确认。",
        review_status: "pending_user_confirmation",
      },
    ],
  },
};

const model = buildRecommendationPageModel(payload, decisions, "ashare");
assert.equal(model.marketTabs.length, 2);
assert.deepEqual(
  model.marketTabs.map((tab) => [tab.id, tab.label, tab.count]),
  [
    ["ashare", "A 股", 1],
    ["crypto_spot", "数字货币现货", 1],
  ],
);
assert.equal(model.selectedMarket, "ashare");
assert.equal(model.items.length, 1);
assert.equal(model.items[0].actionLabel, "候选买入");
assert.equal(model.items[0].scoreDisplay, "86.45");
assert.equal(model.items[0].confidenceDisplay, "74%");
assert.equal(model.items[0].riskCount, 2);
assert.equal(model.items[0].pendingDecision?.decisionId, "decision:1");
assert.equal(model.items[0].reportWorkflowRunId, "workflow:report:1");
assert.equal(model.avoidPoolSummary.count, 2);
assert.match(model.avoidPoolSummary.description, /本次运行剔除回避池 2 项/);
assert.deepEqual(model.items[0].scoreBreakdown.map((item) => item.group), [
  "technical",
  "liquidity",
  "risk",
]);
assert.equal(model.items[0].riskRebuttal, "短期波动升高，仓位需要受限。");

const normalized = normalizeRecommendationItem(payload.recommendations[0], decisions.data.items);
assert.equal(normalized?.assetLabel, "000001 平安银行");
assert.equal(normalized?.marketLabel, "A 股");

assert.deepEqual(buildDecisionFeedbackPayload("accepted", "确认采纳"), {
  feedback: "accepted",
  comment: "确认采纳",
});

assert.deepEqual(buildDecisionFeedbackPayload("modified", "改为观察", "watch_only"), {
  feedback: "modified",
  comment: "改为观察",
  modified_action: "watch_only",
});

const merged = mergeRecommendationPayloads([
  {
    status: "ok",
    active_run: { run_id: "run:a", market: "ashare", payload: { avoid_pool_excluded: { count: 1 } } },
    runs: [{ run_id: "run:a", market: "ashare", payload: { avoid_pool_excluded: { count: 1 } } }],
    recommendations: [{ ...payload.recommendations[0], recommendation_id: "rec:a", market: "ashare" }],
    metrics: { recommendation_count: 1 },
  },
  {
    status: "ok",
    active_run: { run_id: "run:c", market: "crypto_spot", payload: { avoid_pool_excluded: { count: 3 } } },
    runs: [{ run_id: "run:c", market: "crypto_spot", payload: { avoid_pool_excluded: { count: 3 } } }],
    recommendations: [{ ...payload.recommendations[1], recommendation_id: "rec:c", market: "crypto_spot" }],
    metrics: { recommendation_count: 1 },
  },
]);

const cryptoModel = buildRecommendationPageModel(merged, decisions, "crypto_spot");
assert.equal(cryptoModel.marketTabs.length, 2);
assert.equal(cryptoModel.items.length, 1);
assert.equal(cryptoModel.activeRun?.runId, "run:c");
assert.equal(cryptoModel.avoidPoolSummary.count, 3);
