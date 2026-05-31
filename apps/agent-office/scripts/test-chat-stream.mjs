import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const nodeReadyOutput = outputText.replace(
  /import\.meta\.env\.VITE_FINANCE_AGENT_API_BASE/g,
  '"http://127.0.0.1:8000"',
);
const moduleUrl = `data:text/javascript;base64,${Buffer.from(nodeReadyOutput).toString("base64")}`;
const { parseChatStreamChunk } = await import(moduleUrl);

const parsed = parseChatStreamChunk(
  'event: status\ndata: {"message":"正在查询"}\n\nevent: tool_call\ndata: {"tool":"recommendation.get_latest"}\n\nevent: tool_result\ndata: {"tool":"recommendation.get_latest","status":"ok"}\n\nevent: delta\ndata: {"content":"第一段"}\n\n',
);

assert.deepEqual(parsed.events, [
  { event: "status", data: { message: "正在查询" } },
  { event: "tool_call", data: { tool: "recommendation.get_latest" } },
  { event: "tool_result", data: { tool: "recommendation.get_latest", status: "ok" } },
  { event: "delta", data: { content: "第一段" } },
]);
assert.equal(parsed.remainder, "");

const partial = parseChatStreamChunk('event: delta\ndata: {"content":"未完成"', "");
assert.deepEqual(partial.events, []);
assert.equal(partial.remainder, 'event: delta\ndata: {"content":"未完成"');

const completed = parseChatStreamChunk('}\n\n', partial.remainder);
assert.deepEqual(completed.events, [{ event: "delta", data: { content: "未完成" } }]);
assert.equal(completed.remainder, "");
