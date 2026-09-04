import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Buffer } from "node:buffer";
import ts from "typescript";

const source = await readFile(new URL("../src/positionMonitorView.ts", import.meta.url), "utf8");
const pageSource = await readFile(new URL("../src/pages/PortfolioPage.tsx", import.meta.url), "utf8");
const styleSource = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const { buildPositionMonitorModel, monitoringActionLabel } = await import(moduleUrl);

assert.equal(monitoringActionLabel("unexecutable"), "暂不可执行");
const model = buildPositionMonitorModel({
  status: "ok",
  positions: [
    {
      position_id: "position:1",
      asset_id: "ashare:600519",
      symbol: "600519",
      name: "贵州茅台",
      monitoring: {
        action: "unexecutable",
        intended_action: "exit",
        execution_status: "blocked",
        severity: "critical",
        sellable_quantity: "0",
        planned_horizon_days: 10,
        structure_direction: "bearish",
        protective_price: "9.50",
        sector_regime: "cooling",
        reason_codes: ["t1_not_sellable"],
      },
    },
  ],
});
assert.equal(model.items[0].assetLabel, "600519 贵州茅台");
assert.equal(model.items[0].intendedAction, "exit");
assert.equal(model.items[0].sellableQuantity, 0);
assert.equal(model.items[0].executionStatus, "blocked");
assert.equal(model.urgentCount, 1);
assert.match(pageSource, /当前动作/);
assert.match(pageSource, /暂不可执行/);
assert.match(pageSource, /T\+1/);
assert.match(styleSource, /\.position-monitor-card/);
