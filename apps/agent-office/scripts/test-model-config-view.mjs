import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/modelConfigView.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
const {
  buildProviderSummaries,
  buildModelOptionsForProvider,
  modelRoleLabel,
  openAICompatibleProviderKeyForModel,
  routeStatusLabel,
  summarizeOpenAICompatibleConfig,
} = await import(moduleUrl);

const models = {
  providers: [
    {
      provider_key: "deepseek-main",
      provider_vendor: "deepseek",
      provider_name: "DeepSeek 主账号",
      base_url: "https://api.deepseek.com/v1",
      is_enabled: true,
    },
    {
      provider_key: "openai-review",
      provider_vendor: "openai",
      provider_name: "OpenAI 复核账号",
      is_enabled: false,
    },
  ],
  models: [
    {
      model_key: "deepseek-v4-pro",
      provider_key: "deepseek-main",
      model_name: "DeepSeek V4 Pro",
      role: "primary_financial_analyst",
      is_enabled: true,
    },
    {
      model_key: "gpt-5.5-pro",
      provider_key: "openai-review",
      model_name: "GPT-5.5 Pro",
      role: "high_risk_reviewer",
      is_enabled: true,
    },
  ],
};

assert.deepEqual(
  buildProviderSummaries(models).map((provider) => [
    provider.providerKey,
    provider.vendor,
    provider.name,
    provider.modelCount,
    provider.statusLabel,
    provider.statusTone,
  ]),
  [
    ["deepseek-main", "deepseek", "DeepSeek 主账号", 1, "已配置", "green"],
    ["openai-review", "openai", "OpenAI 复核账号", 1, "已停用", "red"],
  ],
);

assert.deepEqual(
  buildModelOptionsForProvider(models, "deepseek-main").map((model) => model.model_key),
  ["deepseek-v4-pro"],
);

assert.equal(modelRoleLabel("primary_financial_analyst"), "主分析 Agent");
assert.equal(modelRoleLabel("high_risk_reviewer"), "高风险复核 Agent");
assert.equal(routeStatusLabel({ ready: true, configured: true }), "已就绪");
assert.equal(routeStatusLabel({ ready: false, configured: true }), "未就绪");
assert.equal(routeStatusLabel(null), "未配置");
assert.equal(openAICompatibleProviderKeyForModel("gpt-5.5"), "openai-compatible:gpt-5.5");
assert.equal(openAICompatibleProviderKeyForModel("qwen/max"), "openai-compatible:qwen-max");

const openAIConfig = summarizeOpenAICompatibleConfig({
  providers: [
    {
      provider_key: "legacy-deepseek",
      provider_vendor: "deepseek",
      provider_name: "DeepSeek",
      base_url: "https://api.deepseek.com/v1",
      api_key: "test-api-key-openai",
      is_enabled: true,
    },
    {
      provider_key: "openai-compatible",
      provider_vendor: "openai_compatible",
      provider_name: "OpenAI 兼容接入",
      base_url: "https://proxy.example.com/v1",
      api_key: "test-api-key-openai",
      is_enabled: true,
    },
  ],
  models: [
    {
      model_key: "qwen-plus",
      provider_key: "openai-compatible",
      model_name: "Qwen Plus",
      role: "primary_financial_analyst",
      is_enabled: true,
    },
    {
      model_key: "disabled-model",
      provider_key: "openai-compatible",
      model_name: "Disabled",
      is_enabled: false,
    },
  ],
});

assert.deepEqual(
  {
    providerKey: openAIConfig.providerKey,
    displayName: openAIConfig.displayName,
    baseUrl: openAIConfig.baseUrl,
    apiKeyPreview: openAIConfig.apiKeyPreview,
    apiKeyConfigured: openAIConfig.apiKeyConfigured,
    enabledModelKeys: openAIConfig.models.map((model) => model.model_key),
  },
  {
    providerKey: "openai-compatible",
    displayName: "OpenAI 兼容接入",
    baseUrl: "https://proxy.example.com/v1",
    apiKeyPreview: "test-api-key-openai",
    apiKeyConfigured: true,
    enabledModelKeys: ["qwen-plus"],
  },
);

const openAIConfigWithMaskedSecretFlag = summarizeOpenAICompatibleConfig({
  providers: [
    {
      provider_key: "openai-compatible",
      provider_vendor: "openai_compatible",
      provider_name: "OpenAI 兼容接入",
      base_url: "https://proxy.example.com/v1",
      api_key: "***",
      api_key_configured: true,
      is_enabled: true,
    },
  ],
  models: [],
});

assert.equal(openAIConfigWithMaskedSecretFlag.apiKeyConfigured, true);
assert.equal(openAIConfigWithMaskedSecretFlag.apiKeyPreview, "***");

const openAIConfigWithMaskedSecretOnly = summarizeOpenAICompatibleConfig({
  providers: [
    {
      provider_key: "openai-compatible",
      provider_vendor: "openai_compatible",
      provider_name: "OpenAI 兼容接入",
      base_url: "https://proxy.example.com/v1",
      api_key: "***",
      is_enabled: true,
    },
  ],
  models: [],
});

assert.equal(openAIConfigWithMaskedSecretOnly.apiKeyConfigured, false);

const oneToOneOpenAIConfig = summarizeOpenAICompatibleConfig({
  providers: [
    {
      provider_key: "openai-compatible:deepseek-v4-pro",
      provider_vendor: "openai_compatible",
      provider_name: "deepseek-v4-pro 接入",
      base_url: "https://deepseek.example.com/v1",
      api_key: "test-api-key-deepseek",
      api_key_configured: true,
      is_enabled: true,
    },
    {
      provider_key: "openai-compatible:gpt-5.5",
      provider_vendor: "openai_compatible",
      provider_name: "gpt-5.5 接入",
      base_url: "https://gpt.example.com/v1",
      api_key: "test-api-key-gpt",
      api_key_configured: true,
      is_enabled: true,
    },
    {
      provider_key: "smoke-web-provider",
      provider_vendor: "openai_compatible",
      provider_name: "Smoke Web Provider",
      base_url: "https://example.invalid/v1",
      api_key: "test-api-key-smoke",
      api_key_configured: true,
      is_enabled: true,
    },
  ],
  models: [
    {
      model_key: "deepseek-v4-pro",
      provider_key: "openai-compatible:deepseek-v4-pro",
      model_name: "deepseek-v4-pro",
      role: "primary_financial_analyst",
      is_enabled: true,
    },
    {
      model_key: "gpt-5.5",
      provider_key: "openai-compatible:gpt-5.5",
      model_name: "gpt-5.5",
      role: "high_risk_reviewer",
      is_enabled: true,
    },
    {
      model_key: "smoke-web-primary",
      provider_key: "smoke-web-provider",
      model_name: "Smoke Web Primary",
      is_enabled: true,
    },
  ],
});

assert.deepEqual(
  oneToOneOpenAIConfig.models.map((model) => [model.model_key, model.provider_key]),
  [
    ["deepseek-v4-pro", "openai-compatible:deepseek-v4-pro"],
    ["gpt-5.5", "openai-compatible:gpt-5.5"],
  ],
);

const emptyOpenAIConfig = summarizeOpenAICompatibleConfig({
  providers: [
    {
      provider_key: "legacy-deepseek",
      provider_vendor: "openai_compatible",
      provider_name: "历史接入",
      base_url: "https://legacy.example.com/v1",
      api_key: "test-api-key-openai",
      is_enabled: true,
    },
  ],
  models: [
    {
      model_key: "legacy-model",
      provider_key: "legacy-deepseek",
      model_name: "Legacy Model",
      is_enabled: true,
    },
  ],
});

assert.deepEqual(
  {
    providerKey: emptyOpenAIConfig.providerKey,
    displayName: emptyOpenAIConfig.displayName,
    baseUrl: emptyOpenAIConfig.baseUrl,
    apiKeyConfigured: emptyOpenAIConfig.apiKeyConfigured,
    modelCount: emptyOpenAIConfig.models.length,
  },
  {
    providerKey: "openai-compatible",
    displayName: "OpenAI 兼容接入",
    baseUrl: "",
    apiKeyConfigured: false,
    modelCount: 0,
  },
);
