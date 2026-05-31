import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Buffer } from "node:buffer";
import ts from "typescript";

const source = await readFile(new URL("../src/agentModelRoutes.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const { buildAgentModelNodes } = await import(moduleUrl);

const nodes = buildAgentModelNodes(
  {
    models: [
      {
        model_key: "deepseek-v4-pro",
        model_name: "DeepSeek V4 Pro",
        provider_key: "deepseek",
        role: "primary_financial_analyst",
        is_enabled: true,
      },
      {
        model_key: "gpt-5.5-pro",
        model_name: "GPT-5.5 Pro",
        provider_key: "openai",
        role: "high_risk_reviewer",
        is_enabled: true,
      },
    ],
    routes: [
      {
        role: "primary_financial_analyst",
        task: "*",
        model_key: "deepseek-v4-pro",
        is_enabled: true,
      },
    ],
  },
  [
    {
      role: "high_risk_reviewer",
      task: "high_risk_review",
      model_key: "gpt-5.5-pro",
      model_name: "GPT-5.5 Pro",
      provider: "openai",
      ready: true,
    },
  ],
);

assert.equal(nodes.length, 2);
assert.deepEqual(
  nodes.map((node) => [node.role, node.label, node.modelKey, node.modelName, node.provider, node.status]),
  [
    [
      "primary_financial_analyst",
      "主分析 Agent",
      "deepseek-v4-pro",
      "DeepSeek V4 Pro",
      "deepseek",
      "已就绪",
    ],
    [
      "high_risk_reviewer",
      "高风险复核 Agent",
      "gpt-5.5-pro",
      "GPT-5.5 Pro",
      "openai",
      "已就绪",
    ],
  ],
);
