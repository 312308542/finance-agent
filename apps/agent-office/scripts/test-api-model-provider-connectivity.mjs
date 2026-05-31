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
const { buildModelProviderConnectivityPayload } = await import(moduleUrl);

assert.deepEqual(
  buildModelProviderConnectivityPayload({
    providerKey: "openai-compatible:gpt-4.1",
    modelKey: "gpt-4.1",
    modelName: "GPT-4.1",
    baseUrl: " https://api.example.com/v1 ",
    apiKey: "test-api-key-live",
    apiKeyIsPreview: false,
  }),
  {
    provider_key: "openai-compatible:gpt-4.1",
    model_key: "gpt-4.1",
    model_name: "GPT-4.1",
    base_url: "https://api.example.com/v1",
    api_key: "test-api-key-live",
    timeout_seconds: 30,
  },
);

assert.deepEqual(
  buildModelProviderConnectivityPayload({
    providerKey: "openai-compatible:gpt-4.1",
    modelKey: "gpt-4.1",
    modelName: "",
    baseUrl: "https://api.example.com/v1",
    apiKey: "sk-8***e9ae",
    apiKeyIsPreview: true,
  }),
  {
    provider_key: "openai-compatible:gpt-4.1",
    model_key: "gpt-4.1",
    model_name: "gpt-4.1",
    base_url: "https://api.example.com/v1",
    api_key: null,
    timeout_seconds: 30,
  },
);
