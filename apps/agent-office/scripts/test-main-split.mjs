import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const mainSource = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");

for (const componentName of [
  "OverviewPage",
  "TaskMonitorPage",
  "DataSyncControlPanel",
  "OpenAICompatibleModelPanel",
  "ChatDock",
]) {
  assert.ok(
    !mainSource.includes(`function ${componentName}(`),
    `main.tsx 不应继续内联定义 ${componentName}`,
  );
}

for (const importPath of [
  './pages/OverviewPage',
  './pages/DetailPage',
  './components/ChatDock',
  './components/consoleCommon',
]) {
  assert.ok(mainSource.includes(importPath), `main.tsx 应从 ${importPath} 导入拆分后的组件`);
}
