type AnyRecord = Record<string, any>;

export type ProviderSummary = {
  providerKey: string;
  vendor: string;
  name: string;
  baseUrl: string;
  enabled: boolean;
  modelCount: number;
  statusLabel: string;
  statusTone: "green" | "amber" | "red";
};

export const OPENAI_COMPATIBLE_PROVIDER_KEY = "openai-compatible";
export const OPENAI_COMPATIBLE_PROVIDER_NAME = "OpenAI 兼容接入";

export type OpenAICompatibleConfig = {
  providerKey: string;
  displayName: string;
  baseUrl: string;
  apiKeyPreview: string;
  apiKeyConfigured: boolean;
  isEnabled: boolean;
  models: AnyRecord[];
};

function asArray(value: unknown): AnyRecord[] {
  return Array.isArray(value) ? (value as AnyRecord[]) : [];
}

function toText(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

export function openAICompatibleProviderKeyForModel(modelKey: string): string {
  const normalized = modelKey
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized ? `${OPENAI_COMPATIBLE_PROVIDER_KEY}:${normalized}` : OPENAI_COMPATIBLE_PROVIDER_KEY;
}

function isOpenAICompatibleProvider(provider: AnyRecord | null | undefined): boolean {
  const providerKey = toText(provider?.provider_key);
  return (
    providerKey === OPENAI_COMPATIBLE_PROVIDER_KEY ||
    providerKey.startsWith(`${OPENAI_COMPATIBLE_PROVIDER_KEY}:`)
  );
}

export function buildProviderSummaries(models?: Record<string, any> | null): ProviderSummary[] {
  const providers = asArray(models?.providers);
  const modelItems = asArray(models?.models);
  const providerItems: AnyRecord[] = providers.length
    ? providers
    : uniqueRawProviders(
        modelItems.map((model) => ({
          provider_key: toText(model.provider_key) || toText(model.provider) || "default-provider",
          provider_vendor: toText(model.provider) || toText(model.provider_key) || "openai_compatible",
          provider_name: toText(model.provider_name) || toText(model.provider_key) || "Default Provider",
          base_url: "",
          is_enabled: model.is_enabled !== false,
        })),
      );

  return providerItems.map((provider) => {
    const providerKey =
      toText(provider.provider_key) ||
      toText(provider.provider) ||
      toText(provider.provider_vendor) ||
      "default-provider";
    const modelCount = modelItems.filter((model) => {
      const modelProviderKey = toText(model.provider_key) || toText(model.provider);
      return modelProviderKey === providerKey;
    }).length;
    const enabled = provider.is_enabled !== false;
    const baseUrl = toText(provider.base_url);
    const configured = enabled && (Boolean(baseUrl) || modelCount > 0);

    return {
      providerKey,
      vendor: toText(provider.provider_vendor) || toText(provider.provider) || "openai_compatible",
      name: toText(provider.provider_name) || toText(provider.name) || providerKey,
      baseUrl,
      enabled,
      modelCount,
      statusLabel: !enabled ? "已停用" : configured ? "已配置" : "待配置",
      statusTone: !enabled ? "red" : configured ? "green" : "amber",
    };
  });
}

function uniqueRawProviders(items: AnyRecord[]): AnyRecord[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = toText(item.provider_key) || toText(item.provider);
    if (!key || seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

export function buildModelOptionsForProvider(
  models: Record<string, any> | null | undefined,
  providerKey: string,
): AnyRecord[] {
  const modelItems = asArray(models?.models);
  if (!providerKey) {
    return modelItems;
  }
  return modelItems.filter((model) => {
    const modelProviderKey = toText(model.provider_key) || toText(model.provider);
    return modelProviderKey === providerKey;
  });
}

export function summarizeOpenAICompatibleConfig(
  models?: Record<string, any> | null,
): OpenAICompatibleConfig {
  const providers = asArray(models?.providers);
  const modelItems = asArray(models?.models);
  const providerByKey = new Map<string, AnyRecord>();
  providers.forEach((item) => {
    const key = toText(item.provider_key);
    if (key) {
      providerByKey.set(key, item);
    }
  });
  const provider =
    providers.find((item) => toText(item.provider_key) === OPENAI_COMPATIBLE_PROVIDER_KEY) ??
    null;
  const endpointModels = modelItems.filter((model) => {
    const modelProviderKey = toText(model.provider_key) || toText(model.provider);
    const modelProvider = providerByKey.get(modelProviderKey);
    return isOpenAICompatibleProvider(modelProvider) && model.is_enabled !== false;
  });
  const apiKeyText = toText(provider?.api_key);
  const apiKeyConfigured =
    provider?.api_key_configured === true ||
    (Boolean(apiKeyText) && apiKeyText !== "***");

  return {
    providerKey: OPENAI_COMPATIBLE_PROVIDER_KEY,
    displayName: toText(provider?.provider_name) || OPENAI_COMPATIBLE_PROVIDER_NAME,
    baseUrl: toText(provider?.base_url),
    apiKeyPreview: apiKeyText,
    apiKeyConfigured,
    isEnabled: provider?.is_enabled !== false,
    models: endpointModels,
  };
}

export function modelRoleLabel(role: string | null | undefined): string {
  if (role === "primary_financial_analyst") {
    return "主分析 Agent";
  }
  if (role === "high_risk_reviewer") {
    return "高风险复核 Agent";
  }
  return role || "未指定";
}

export function routeStatusLabel(route: AnyRecord | null | undefined): string {
  if (!route) {
    return "未配置";
  }
  if (route.ready === false) {
    return "未就绪";
  }
  if (route.configured === false) {
    return "未配置";
  }
  return "已就绪";
}
