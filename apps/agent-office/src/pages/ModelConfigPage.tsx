import * as React from "react";
import { App, Button, Input, Select } from "antd";
import { Check, Eye, EyeOff, Pencil, Plus, Settings2, Trash2, Wifi } from "lucide-react";
import { buildModelProviderConnectivityPayload, deleteModelInstance, revealModelProviderSecret, saveModelInstance, saveModelProvider, saveModelRoute, testModelProviderConnectivity } from "../api";
import type { ConsolePageProps } from "../consoleTypes";
import { Panel } from "../components/consoleCommon";
import { OPENAI_COMPATIBLE_PROVIDER_KEY, OPENAI_COMPATIBLE_PROVIDER_NAME, modelRoleLabel, openAICompatibleProviderKeyForModel, routeStatusLabel, summarizeOpenAICompatibleConfig } from "../modelConfigView";
import { AgentSummaryPanel, DataHealthPanel } from "./OverviewPage";

export function ModelConfigPage({
  models,
  modelPreview,
  refresh,
  refreshModelPreview,
  workflows,
  dataHealth,
}: Pick<ConsolePageProps, "models" | "modelPreview" | "refresh" | "refreshModelPreview" | "workflows" | "dataHealth">) {
  return (
    <section className="page-grid">
      <OpenAICompatibleModelPanel
        models={models}
        modelPreview={modelPreview}
        refresh={refresh}
        refreshModelPreview={refreshModelPreview}
      />
      <AgentSummaryPanel workflows={workflows} models={models} modelPreview={modelPreview} />
      <DataHealthPanel dataHealth={dataHealth} />
    </section>
  );
}

function OpenAICompatibleModelPanel({
  models,
  modelPreview,
  refresh,
  refreshModelPreview,
}: Pick<ConsolePageProps, "models" | "modelPreview" | "refresh" | "refreshModelPreview">) {
  const { modal } = App.useApp();
  const routeItems = Array.isArray(models?.routes) ? models.routes : [];
  const endpoint = React.useMemo(() => summarizeOpenAICompatibleConfig(models), [models]);
  const endpointModelItems = endpoint.models;
  const providerItems = Array.isArray(models?.providers) ? models.providers : [];
  const providerByKey = React.useMemo(
    () => {
      const items = new Map<string, any>();
      providerItems.forEach((provider: any) => {
        if (provider.provider_key) {
          items.set(provider.provider_key, provider);
        }
      });
      return items;
    },
    [providerItems],
  );
  const primaryRoute = findRoute(routeItems, "primary_financial_analyst");
  const reviewRoute = findRoute(routeItems, "high_risk_reviewer");
  const previewRoutes = Array.isArray(modelPreview?.data?.routes) ? modelPreview.data.routes : [];
  const previewPrimaryRoute = findRoute(previewRoutes, "primary_financial_analyst");
  const previewReviewRoute = findRoute(previewRoutes, "high_risk_reviewer");
  const [endpointName, setEndpointName] = React.useState(endpoint.displayName);
  const [baseUrl, setBaseUrl] = React.useState(endpoint.baseUrl);
  const [apiKey, setApiKey] = React.useState("");
  const [apiKeyIsPreview, setApiKeyIsPreview] = React.useState(false);
  const [apiKeyVisible, setApiKeyVisible] = React.useState(false);
  const [secretProviderKey, setSecretProviderKey] = React.useState(endpoint.providerKey);
  const [editingModelKey, setEditingModelKey] = React.useState<string | null>(null);
  const [modelKey, setModelKey] = React.useState("");
  const [modelName, setModelName] = React.useState("");
  const [modelRole, setModelRole] = React.useState("primary_financial_analyst");
  const [primaryModelKey, setPrimaryModelKey] = React.useState("");
  const [reviewModelKey, setReviewModelKey] = React.useState("");
  const [saveStatus, setSaveStatus] = React.useState("未保存");
  const [isSaving, setIsSaving] = React.useState(false);
  const [isTestingConnectivity, setIsTestingConnectivity] = React.useState(false);
  const normalizedModelKey = modelKey.trim();
  const normalizedModelName = modelName.trim();
  const activeProviderKey = normalizedModelKey
    ? openAICompatibleProviderKeyForModel(normalizedModelKey)
    : endpoint.providerKey;
  const hasSavedApiKey = Boolean(apiKey.trim()) || apiKeyIsPreview;
  const endpointReady = Boolean(baseUrl.trim() && (hasSavedApiKey || apiKey.trim()));
  const canSaveEndpoint = Boolean(endpointName.trim() && baseUrl.trim() && (hasSavedApiKey || apiKey.trim()));
  const canTestConnectivity = Boolean(normalizedModelKey && baseUrl.trim() && (hasSavedApiKey || apiKey.trim()));
  const canSaveInstance = Boolean(normalizedModelKey && normalizedModelName);
  const canSaveRoutes = Boolean(primaryModelKey && reviewModelKey);

  React.useEffect(() => {
    if (editingModelKey) {
      return;
    }
    const firstModel = endpointModelItems[0];
    setModelKey(firstModel?.model_key ?? "");
    setModelName(firstModel?.model_name ?? "");
    setModelRole(firstModel?.role ?? "primary_financial_analyst");
    if (firstModel) {
      applyEndpointForm(providerEndpointForModel(firstModel));
      return;
    }
    syncEndpointFromSavedProvider();
  }, [editingModelKey, endpointModelItems[0]?.model_key, providerByKey]);

  React.useEffect(() => {
    const modelKeys = new Set(endpointModelItems.map((item: any) => item.model_key));
    const pickModel = (route: any, fallbackIndex: number) => {
      if (route?.model_key && modelKeys.has(route.model_key)) {
        return route.model_key;
      }
      return endpointModelItems[fallbackIndex]?.model_key ?? endpointModelItems[0]?.model_key ?? "";
    };
    setPrimaryModelKey(pickModel(primaryRoute, 0));
    setReviewModelKey(pickModel(reviewRoute, 1));
  }, [primaryRoute?.model_key, reviewRoute?.model_key, endpointModelItems.length]);

  const afterSave = async (result: Record<string, any>, message: string) => {
    if (result.status !== "ok") {
      setSaveStatus(result.message ?? "保存失败");
      return false;
    }
    setSaveStatus(message);
    await refresh?.();
    await refreshModelPreview?.();
    return true;
  };

  const providerEndpointForModel = (item: any) => {
    const modelProviderKey = item?.provider_key ?? "";
    const provider =
      providerByKey.get(modelProviderKey) ??
      providerByKey.get(openAICompatibleProviderKeyForModel(item?.model_key ?? "")) ??
      null;
    if (!provider) {
      return {
        displayName: item?.model_name ? `${item.model_name} 接入` : OPENAI_COMPATIBLE_PROVIDER_NAME,
        baseUrl: "",
        apiKeyPreview: "",
        apiKeyConfigured: false,
        secretProviderKey: activeProviderKey,
      };
    }
    return {
      displayName: provider.provider_name ?? OPENAI_COMPATIBLE_PROVIDER_NAME,
      baseUrl: provider.base_url ?? "",
      apiKeyPreview: provider.api_key ?? "",
      apiKeyConfigured:
        provider.api_key_configured === true ||
        (Boolean(provider.api_key) && provider.api_key !== "***"),
      secretProviderKey: provider.provider_key ?? activeProviderKey,
    };
  };

  const applyEndpointForm = (data: {
    displayName: string;
    baseUrl: string;
    apiKeyPreview: string;
    apiKeyConfigured: boolean;
    secretProviderKey: string;
  }) => {
    setEndpointName(data.displayName);
    setBaseUrl(data.baseUrl);
    setApiKey(data.apiKeyPreview);
    setApiKeyIsPreview(Boolean(data.apiKeyPreview));
    setApiKeyVisible(false);
    setSecretProviderKey(data.secretProviderKey);
  };

  const saveEndpoint = async (message = "接入端点已保存") => {
    if (!endpointName.trim()) {
      setSaveStatus("请填写接入名称");
      return false;
    }
    if (!baseUrl.trim()) {
      setSaveStatus("请填写 Base URL");
      return false;
    }
    if (!hasSavedApiKey && !apiKey.trim()) {
      setSaveStatus("请填写 API Key");
      return false;
    }
    const normalizedApiKey = apiKeyIsPreview ? null : apiKey.trim() || null;
    const result = await saveModelProvider(activeProviderKey, {
      provider_vendor: "openai_compatible",
      provider_name: endpointName.trim() || OPENAI_COMPATIBLE_PROVIDER_NAME,
      base_url: baseUrl.trim() || null,
      api_key: normalizedApiKey,
      timeout_seconds: 30,
      is_enabled: true,
      is_default: true,
    });
    return afterSave(result, message);
  };

  const revealApiKey = async () => {
    if (apiKeyVisible) {
      setApiKeyVisible(false);
      return;
    }
    if (apiKey.trim() && !apiKeyIsPreview) {
      setApiKeyVisible(true);
      return;
    }
    const result = await revealModelProviderSecret(secretProviderKey || activeProviderKey);
    if (result.status !== "ok") {
      setSaveStatus(result.message ?? "读取 API Key 失败");
      return;
    }
    const revealed = result.data?.api_key ?? "";
    if (!revealed) {
      setSaveStatus("当前接入还没有保存 API Key");
      return;
    }
    setApiKey(revealed);
    setApiKeyIsPreview(false);
    setApiKeyVisible(true);
    setSaveStatus("API Key 已解密显示");
  };

  const testConnectivity = async () => {
    if (!canTestConnectivity) {
      setSaveStatus("请先填写模型 ID、Base URL 和 API Key");
      return;
    }
    setIsTestingConnectivity(true);
    setSaveStatus("正在测试连通性...");
    try {
      const result = await testModelProviderConnectivity(
        buildModelProviderConnectivityPayload({
          providerKey: activeProviderKey,
          modelKey: normalizedModelKey,
          modelName: normalizedModelName,
          baseUrl,
          apiKey,
          apiKeyIsPreview,
        }),
      );
      if (result.status === "ok") {
        const latency = result.data?.latency_ms;
        const httpStatus = result.data?.http_status;
        setSaveStatus(`连通性正常 · HTTP ${httpStatus ?? 200} · ${latency ?? "-"}ms`);
        return;
      }
      setSaveStatus(result.message ?? "连通性测试失败");
    } finally {
      setIsTestingConnectivity(false);
    }
  };

  const syncEndpointFromSavedProvider = () => {
    applyEndpointForm({
      ...endpoint,
      secretProviderKey: endpoint.providerKey,
    });
  };

  const saveInstance = async () => {
    if (!(await saveEndpoint("接入端点已同步"))) {
      return;
    }
    if (!canSaveInstance) {
      setSaveStatus("请填写模型 ID 和显示名称");
      return;
    }
    const result = await saveModelInstance(normalizedModelKey, {
      provider_key: activeProviderKey,
      model_name: normalizedModelName,
      model_type: "llm",
      role: modelRole,
      route_priority: modelRole === "primary_financial_analyst" ? 120 : 100,
      timeout_seconds: 30,
      is_enabled: true,
    });
    if (result.status === "ok") {
      if (modelRole === "high_risk_reviewer") {
        setReviewModelKey(normalizedModelKey);
      } else {
        setPrimaryModelKey(normalizedModelKey);
      }
    }
    const ok = await afterSave(result, editingModelKey ? "模型已更新" : "模型已新增");
    if (ok) {
      setEditingModelKey(normalizedModelKey);
    }
  };

  const resetModelEditor = () => {
    setEditingModelKey(null);
    setModelKey("");
    setModelName("");
    setModelRole("primary_financial_analyst");
    applyEndpointForm({
      displayName: OPENAI_COMPATIBLE_PROVIDER_NAME,
      baseUrl: "",
      apiKeyPreview: "",
      apiKeyConfigured: false,
      secretProviderKey: "",
    });
    setSaveStatus("准备新增模型");
  };

  const editModel = (item: any) => {
    setEditingModelKey(item.model_key ?? null);
    setModelKey(item.model_key ?? "");
    setModelName(item.model_name ?? "");
    setModelRole(item.role ?? "primary_financial_analyst");
    applyEndpointForm(providerEndpointForModel(item));
    setSaveStatus("正在编辑模型");
  };

  const deleteModel = async (item: any) => {
    const targetKey = item.model_key ?? "";
    if (!targetKey) {
      return;
    }
    modal.confirm({
      title: `停用模型 ${item.model_name ?? targetKey}？`,
      content: "停用后该模型不再参与 Agent 路由，已保存的接入端点与 API Key 不会删除。",
      okText: "停用模型",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        const result = await deleteModelInstance(targetKey);
        if (result.status === "ok") {
          if (editingModelKey === targetKey) {
            resetModelEditor();
          }
          const fallback = endpointModelItems.find((model: any) => model.model_key !== targetKey)?.model_key ?? "";
          if (primaryModelKey === targetKey) {
            setPrimaryModelKey(fallback);
          }
          if (reviewModelKey === targetKey) {
            setReviewModelKey(fallback);
          }
        }
        await afterSave(result, "模型已停用");
      },
    });
  };

  const saveRoutes = async () => {
    if (!canSaveRoutes) {
      setSaveStatus("请先保存可用于 Agent 的模型");
      return;
    }
    const primaryResult = await saveModelRoute("primary_financial_analyst", {
      workflow_type: "*",
      task: "*",
      model_key: primaryModelKey,
      reason: "Web 控制台切换主分析 Agent 模型。",
      priority: 200,
      is_enabled: true,
    });
    if (primaryResult.status !== "ok") {
      await afterSave(primaryResult, "");
      return;
    }
    const reviewResult = await saveModelRoute("high_risk_reviewer", {
      workflow_type: "*",
      task: "high_risk_review",
      model_key: reviewModelKey,
      reason: "Web 控制台切换高风险复核 Agent 模型。",
      priority: 200,
      is_enabled: true,
    });
    await afterSave(reviewResult, "Agent 默认模型已保存");
  };

  const runSaving = async (action: () => Promise<void>) => {
    setIsSaving(true);
    try {
      await action();
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <Panel
      title="OpenAI 兼容模型"
      subtitle="自定义模型端点、模型目录和 Agent 默认模型"
      icon={<Settings2 size={16} />}
    >
      <div className="openai-model-console">
        <section className="endpoint-panel">
          <div className="model-section-head">
            <div>
              <h3>接入端点</h3>
              <p>{endpointReady ? "已连接" : "待配置"}</p>
            </div>
            <span className={`status-pill tone-${endpointReady ? "green" : "amber"}`}>
              {endpointReady ? "可用" : "缺少配置"}
            </span>
          </div>
          <div className="endpoint-form-grid">
            <label>
              <span>名称</span>
              <Input value={endpointName} onChange={(event) => setEndpointName(event.target.value)} />
            </label>
            <label className="endpoint-form-wide">
              <span>Base URL</span>
              <Input
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder="https://api.example.com/v1"
              />
            </label>
            <label className="endpoint-form-wide">
              <span>API Key</span>
              <div className="secret-input-row">
                <Input
                  value={apiKey}
                  onChange={(event) => {
                    setApiKey(event.target.value);
                    setApiKeyIsPreview(false);
                  }}
                  placeholder={hasSavedApiKey ? "已保存，留空不改" : "sk-..."}
                  type={apiKeyVisible ? "text" : "password"}
                  suffix={
                    <button
                      className="secret-toggle-button"
                      type="button"
                      onClick={() => void revealApiKey()}
                      disabled={isSaving || (!hasSavedApiKey && !apiKey.trim())}
                      title={apiKeyVisible ? "隐藏 API Key" : "显示 API Key"}
                    >
                      {apiKeyVisible ? <EyeOff size={15} /> : <Eye size={15} />}
                    </button>
                  }
                />
              </div>
            </label>
          </div>
          <div className="model-action-row">
            <Button
              type="primary"
              icon={<Check size={15} />}
              onClick={() => void runSaving(() => saveEndpoint().then(() => undefined))}
              disabled={!canSaveEndpoint || isSaving}
            >
              保存接入
            </Button>
            <Button
              icon={<Wifi size={15} />}
              onClick={() => void testConnectivity()}
              disabled={!canTestConnectivity || isSaving || isTestingConnectivity}
              loading={isTestingConnectivity}
              title="测试当前模型接入端点"
            >
              测试连通性
            </Button>
            <span>{saveStatus}</span>
          </div>
        </section>

        <section className="model-library-panel">
          <div className="model-section-head">
            <div>
              <h3>模型列表</h3>
              <p>{endpointModelItems.length} 个可用模型</p>
            </div>
            <Button icon={<Plus size={15} />} onClick={resetModelEditor}>
              新增
            </Button>
          </div>
          <div className="model-library-list">
            {endpointModelItems.length > 0 ? (
              endpointModelItems.map((item: any) => (
                <article key={item.model_key} className="openai-model-card">
                  <div>
                    <strong>{item.model_name ?? item.model_key}</strong>
                    <span>{item.model_key}</span>
                  </div>
                  <em>{modelRoleLabel(item.role)}</em>
                  <div className="model-card-actions">
                    <Button
                      size="small"
                      type="text"
                      icon={<Pencil size={14} />}
                      onClick={() => editModel(item)}
                      title="编辑模型"
                    />
                    <Button
                      size="small"
                      type="text"
                      danger
                      icon={<Trash2 size={14} />}
                      onClick={() => void runSaving(() => deleteModel(item))}
                      title="停用模型"
                      disabled={isSaving}
                    />
                  </div>
                </article>
              ))
            ) : (
              <div className="empty-state">暂无模型</div>
            )}
          </div>
        </section>

        <section className="model-editor-panel">
          <div className="model-section-head">
            <div>
              <h3>{editingModelKey ? "编辑模型" : "新增模型"}</h3>
              <p>{editingModelKey ?? "未选择模型"}</p>
            </div>
          </div>
          <label>
            <span>模型 ID</span>
            <Input value={modelKey} onChange={(event) => setModelKey(event.target.value)} placeholder="gpt-4.1" />
          </label>
          <label>
            <span>显示名称</span>
            <Input value={modelName} onChange={(event) => setModelName(event.target.value)} placeholder="GPT-4.1" />
          </label>
          <label>
            <span>默认用途</span>
            <Select
              value={modelRole}
              onChange={(value) => setModelRole(value)}
              options={[
                { value: "primary_financial_analyst", label: "主分析 Agent" },
                { value: "high_risk_reviewer", label: "高风险复核 Agent" },
              ]}
            />
          </label>
          <div className="model-action-row">
            <Button
              type="primary"
              icon={<Check size={15} />}
              onClick={() => void runSaving(saveInstance)}
              disabled={!canSaveInstance || isSaving}
            >
              保存模型
            </Button>
            <Button icon={<Plus size={15} />} onClick={resetModelEditor}>
              新建
            </Button>
          </div>
        </section>

        <section className="agent-binding-panel">
          <div className="model-section-head">
            <div>
              <h3>Agent 默认模型</h3>
              <p>{canSaveRoutes ? "已选择" : "待选择"}</p>
            </div>
          </div>
          <div className="agent-binding-grid">
            <label>
              <span>主分析 Agent</span>
              <Select
                value={primaryModelKey || undefined}
                onChange={(value) => setPrimaryModelKey(value)}
                disabled={!endpointModelItems.length}
                options={endpointModelItems.map((item: any) => ({
                  value: item.model_key,
                  label: item.model_name ?? item.model_key,
                }))}
              />
              <em>{routeStatusLabel(previewPrimaryRoute ?? primaryRoute)}</em>
            </label>
            <label>
              <span>高风险复核 Agent</span>
              <Select
                value={reviewModelKey || undefined}
                onChange={(value) => setReviewModelKey(value)}
                disabled={!endpointModelItems.length}
                options={endpointModelItems.map((item: any) => ({
                  value: item.model_key,
                  label: item.model_name ?? item.model_key,
                }))}
              />
              <em>{routeStatusLabel(previewReviewRoute ?? reviewRoute)}</em>
            </label>
          </div>
          <div className="model-action-row">
            <Button
              type="primary"
              icon={<Check size={15} />}
              onClick={() => void runSaving(saveRoutes)}
              disabled={!canSaveRoutes || isSaving}
            >
              保存默认模型
            </Button>
          </div>
          <div className="route-preview">
            <strong>当前生效</strong>
            {previewRoutes.length > 0 ? (
              previewRoutes.map((route: any) => (
                <p key={`${route.role}-${route.task}-${route.model_key}`}>
                  {modelRoleLabel(route.role)}
                  {" -> "}
                  {route.model_key} / {routeStatusLabel(route)}
                </p>
              ))
            ) : (
              <p>暂无路由</p>
            )}
          </div>
        </section>
      </div>
    </Panel>
  );
}

function findRoute(routes: any[], role: string) {
  return routes.find((item) => item.role === role && item.is_enabled !== false);
}
