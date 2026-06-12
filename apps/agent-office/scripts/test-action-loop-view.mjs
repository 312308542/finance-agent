import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/actionLoopView.ts", import.meta.url), "utf8");
const apiSource = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
const recommendationSource = await readFile(
  new URL("../src/pages/RecommendationPage.tsx", import.meta.url),
  "utf8",
);
const portfolioSource = await readFile(
  new URL("../src/pages/PortfolioPage.tsx", import.meta.url),
  "utf8",
);
const riskSource = await readFile(new URL("../src/pages/RiskPage.tsx", import.meta.url), "utf8");

const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const {
  buildExecutionPayload,
  buildOrderDraftsModel,
  buildUpcomingReviewsModel,
  normalizeOrderDraft,
  normalizeExecutionRecord,
} = await import(moduleUrl);

const draftPayload = {
  status: "ok",
  data: {
    items: [
      {
        order_draft_id: "draft:1",
        owner_id: "owner:demo",
        portfolio_id: "portfolio:demo",
        asset_id: "ashare:600519",
        market: "ashare",
        decision_log_id: "decision:1",
        action: "buy",
        suggested_price_range: { low: "1680.00", high: "1720.00" },
        suggested_position_ratio: "0.100000",
        constraints: { stop_loss: "1580.00" },
        status: "drafted",
        disclaimer: "非投资建议，仅用于用户自行决策前的订单草案。",
        created_at: "2026-06-13T10:00:00+08:00",
        updated_at: "2026-06-13T10:00:00+08:00",
      },
    ],
  },
};

const draft = normalizeOrderDraft(draftPayload.data.items[0]);
assert.equal(draft?.orderDraftId, "draft:1");
assert.equal(draft?.assetLabel, "600519");
assert.equal(draft?.priceRangeDisplay, "1680.00 - 1720.00");
assert.equal(draft?.positionRatioDisplay, "10%");
assert.match(draft?.disclaimer ?? "", /非投资建议/);

const draftModel = buildOrderDraftsModel(draftPayload);
assert.equal(draftModel.items.length, 1);
assert.equal(draftModel.metrics.draftedCount, 1);
assert.equal(draftModel.emptyText, "暂无订单草案，确认采纳建议后可生成文档性质操作草案。");

const execution = normalizeExecutionRecord({
  execution_id: "execution:1",
  owner_id: "owner:demo",
  portfolio_id: "portfolio:demo",
  asset_id: "ashare:600519",
  market: "ashare",
  order_draft_id: "draft:1",
  decision_log_id: "decision:1",
  action: "buy",
  executed_price: "1699.5000000000",
  executed_quantity: "100.0000000000",
  executed_at: "2026-06-13T10:05:00+08:00",
  fee: "5.0000000000",
  note: "已在外部交易软件手工执行。",
  source: "user_reported",
  created_at: "2026-06-13T10:06:00+08:00",
});
assert.equal(execution?.executionId, "execution:1");
assert.equal(execution?.priceDisplay, "1699.50");
assert.equal(execution?.quantityDisplay, "100");
assert.equal(execution?.sourceLabel, "用户手工登记");

assert.deepEqual(
  buildExecutionPayload({
    ownerId: "owner:demo",
    portfolioId: "portfolio:demo",
    assetId: "ashare:600519",
    market: "ashare",
    action: "buy",
    executedPrice: "1699.50",
    executedQuantity: "100",
    executedAt: "2026-06-13T10:05:00+08:00",
    orderDraftId: "draft:1",
    decisionLogId: "decision:1",
    fee: "5",
    note: "外部登记",
  }),
  {
    owner_id: "owner:demo",
    portfolio_id: "portfolio:demo",
    asset_id: "ashare:600519",
    market: "ashare",
    action: "buy",
    executed_price: "1699.50",
    executed_quantity: "100",
    executed_at: "2026-06-13T10:05:00+08:00",
    order_draft_id: "draft:1",
    decision_log_id: "decision:1",
    fee: "5",
    note: "外部登记",
    source: "user_reported",
  },
);

const reviews = buildUpcomingReviewsModel({
  status: "ok",
  data: {
    items: [
      {
        review_task_id: "review:1",
        owner_id: "owner:demo",
        asset_id: "ashare:600519",
        source_decision_id: "decision:1",
        review_type: "execution_outcome",
        due_at: "2026-07-03T10:05:00+08:00",
        status: "pending",
        review_questions: [{ question: "比较执行表现。" }],
        payload: { execution_id: "execution:1" },
      },
    ],
  },
});
assert.equal(reviews.metrics.pendingCount, 1);
assert.equal(reviews.items[0].title, "600519 执行复盘");
assert.match(reviews.items[0].detail, /比较执行表现/);

for (const token of [
  "confirmDecision",
  "createOrderDraft",
  "loadOrderDrafts",
  "recordExecution",
  "loadExecutionRecords",
  "loadUpcomingReviews",
]) {
  assert.match(apiSource, new RegExp(`export async function ${token}`));
}

assert.match(recommendationSource, /生成订单草案/);
assert.match(recommendationSource, /我已在外部执行，去登记/);
assert.match(portfolioSource, /执行登记/);
assert.match(portfolioSource, /执行历史/);
assert.match(riskSource, /待复盘/);

for (const pageSource of [recommendationSource, portfolioSource, riskSource]) {
  assert.doesNotMatch(pageSource, /提交订单|下单|自动交易/);
}
