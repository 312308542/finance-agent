import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Buffer } from "node:buffer";
import ts from "typescript";

const source = await readFile(new URL("../src/reportView.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const {
  buildReportPageModel,
  formatReportStatus,
  formatReviewStatus,
  renderTrustedReportMarkdown,
} = await import(moduleUrl);

const payload = {
  status: "ok",
  items: [
    {
      workflow_run_id: "run-20260612-001",
      workflow_type: "balanced_swing_v1",
      title: "A 股午后复盘",
      summary: "趋势偏弱，等待复核。",
      status: "completed",
      decision_status: "approved",
      review_status: "approved",
      created_at: "2026-06-12T15:30:00+08:00",
      recommendation_count: 5,
      risk_count: 2,
      payload: { primary_asset: "600519" },
    },
    {
      workflow_run_id: "run-20260612-002",
      workflow_type: "risk_review",
      title: "",
      summary: "",
      status: "running",
      review_status: "pending_manual",
      created_at: "2026-06-12T14:10:00+08:00",
    },
  ],
  metrics: {
    total_count: 2,
    reviewed_count: 1,
    pending_review_count: 1,
    rejected_count: 0,
  },
};

const detail = {
  workflow_run_id: "run-20260612-001",
  workflow_type: "balanced_swing_v1",
  report: "# 结论\n\n**建议**：候选观察\n\n- 证据完整\n\n<script>alert('x')</script>",
  report_review_appended: true,
  review_results: [
    {
      status: "approved",
      reviewer: "high_risk_reviewer",
      summary: "高风险复核通过",
    },
  ],
};

assert.deepEqual(formatReportStatus("completed"), {
  label: "已完成",
  tone: "green",
});

assert.deepEqual(formatReviewStatus("pending_manual"), {
  label: "待人工复核",
  tone: "amber",
});

const model = buildReportPageModel(payload, detail, "run-20260612-001");
assert.equal(model.items.length, 2);
assert.equal(model.selected?.workflowRunId, "run-20260612-001");
assert.equal(model.metrics.totalCount, 2);
assert.equal(model.metrics.pendingReviewCount, 1);
assert.equal(model.items[0].title, "A 股午后复盘");
assert.equal(model.items[1].title, "风险复核 报告");
assert.equal(model.detail?.reviewResults.length, 1);
assert.equal(model.detail?.reviewAppended, true);

const fallbackSelection = buildReportPageModel(payload, detail, "missing-run");
assert.equal(fallbackSelection.selected?.workflowRunId, "run-20260612-001");

const rendered = renderTrustedReportMarkdown(detail.report);
assert.match(rendered, /<h1 id="结论">结论<\/h1>/);
assert.match(rendered, /<strong>建议<\/strong>/);
assert.match(rendered, /<li>证据完整<\/li>/);
assert.match(rendered, /&lt;script&gt;alert\('x'\)&lt;\/script&gt;/);
assert.doesNotMatch(rendered, /<script>/);

const emptyModel = buildReportPageModel({ status: "empty", items: [] }, null, null);
assert.equal(emptyModel.selected, null);
assert.equal(emptyModel.emptyText, "暂无报告，等待 Workflow 生成中文报告。");
