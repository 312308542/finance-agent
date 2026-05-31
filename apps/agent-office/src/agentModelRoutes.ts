type AnyRecord = Record<string, any>;

export type AgentModelNode = {
  role: string;
  label: string;
  modelKey: string;
  modelName: string;
  provider: string;
  status: string;
  detail: string;
};

const AGENT_MODEL_DEFS = [
  { role: "primary_financial_analyst", label: "主分析 Agent" },
  { role: "high_risk_reviewer", label: "高风险复核 Agent" },
];

function asArray(value: unknown): AnyRecord[] {
  return Array.isArray(value) ? (value as AnyRecord[]) : [];
}

function toText(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function findByRole(items: AnyRecord[], role: string): AnyRecord | undefined {
  return items.find((item) => item?.role === role && item?.is_enabled !== false);
}

function findByModelKey(items: AnyRecord[], modelKey: string): AnyRecord | undefined {
  if (!modelKey) {
    return undefined;
  }
  return items.find((item) => item?.model_key === modelKey && item?.is_enabled !== false);
}

export function buildAgentModelNodes(
  models?: Record<string, any> | null,
  previewRoutes: AnyRecord[] = [],
): AgentModelNode[] {
  const modelItems = asArray(models?.models);
  const routeItems = asArray(models?.routes);
  const routes = asArray(previewRoutes);

  return AGENT_MODEL_DEFS.map((agent) => {
    const previewRoute = findByRole(routes, agent.role);
    const savedRoute = findByRole(routeItems, agent.role);
    const selectedModelKey =
      toText(previewRoute?.model_key) ||
      toText(savedRoute?.model_key) ||
      toText(findByModelKey(modelItems, toText(previewRoute?.model_key) || toText(savedRoute?.model_key))?.model_key);
    const modelFromRole = findByRole(modelItems, agent.role);
    const modelFromKey = findByModelKey(modelItems, selectedModelKey);
    const model = modelFromKey ?? modelFromRole;
    const configured = Boolean(previewRoute?.configured ?? savedRoute ?? model);
    const ready = previewRoute?.ready ?? (model ? model.is_enabled !== false : false);
    const modelName = toText(previewRoute?.model_name) || toText(model?.model_name) || selectedModelKey || "未配置";
    const provider = toText(previewRoute?.provider) || toText(model?.provider_key) || toText(model?.provider) || "-";
    const detailParts = [
      selectedModelKey ? `模型 ${selectedModelKey}` : "模型未配置",
      provider !== "-" ? `供应商 ${provider}` : "",
      toText(previewRoute?.task) || toText(savedRoute?.task) ? `任务 ${toText(previewRoute?.task) || toText(savedRoute?.task)}` : "",
    ].filter(Boolean);

    const status = !configured || !selectedModelKey ? "未配置" : ready ? "已就绪" : "未就绪";

    return {
      role: agent.role,
      label: agent.label,
      modelKey: selectedModelKey,
      modelName,
      provider,
      status,
      detail: detailParts.join(" · ") || "未配置",
    };
  });
}
