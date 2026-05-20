# Prompt 结构化模板设计

更新时间：2026-05-21

本文定义 `finance-agent` 的 Prompt 模板化方案。目标是把当前写死在 `prompts.py` 中的字符串，升级为可组合、可测试、可审计的代码内结构化模板。本文只设计代码层模板系统，不设计数据库模板表，也不接真实模型调用。

## 1. 背景

当前 Prompt 模块已经完成基础版：

- `build_prompt_bundle()` 已接入 `ModelFinanceAgentPlanner`。
- Workflow 审计已经能保存 `model_prompt_bundle` 和 `model_prompt_envelope`。
- 共享上下文采用 `stable` / `context` / `volatile` 三层。

但当前 Prompt 仍然是字符串常量，存在几个问题：

- 缺少模板元信息，无法区分模板 ID、角色、适用市场和适用 Workflow。
- 缺少结构化输出协议，真实模型接入后不利于解析和回写。
- 工具调用规则、风险反驳规则、A 股与数字货币规则还没有固化。
- 还没有为后续 `prompt_hash`、模板版本和模型调用审计预留稳定接口。

## 2. 设计目标

1. 把 Prompt 从硬编码字符串升级为结构化模板对象。
2. 保持现有 `build_prompt_bundle()` 对外入口稳定，减少调用方改动。
3. 明确工具调用协议：模型不能编造事实，必须基于已入库工具和 Workflow。
4. 明确输出 JSON Schema：字段英文，解释内容中文。
5. 明确风险反驳协议：高风险动作必须经过反驳、降级或补证据。
6. 明确中英文报告约束：结构化字段英文，面向用户的结论和报告中文。
7. 为后续模板版本、模板 hash、模型调用审计预留字段。

## 3. 非目标

本阶段不做以下内容：

- 不新增 `prompt_templates` 数据库表。
- 不新增 `model_prompt_runs` 数据库表。
- 不支持用户在前端动态编辑 Prompt。
- 不实现真实模型调用结果解析。
- 不把 Prompt 拆成大量文件。模板数量稳定前，先保留在 `runtime/prompts.py` 中。

## 4. 参考原则

参考 `hermes-agent` 的系统提示词设计，保留三层 Prompt 结构：

- `stable`：身份、能力边界、工具纪律、输出协议。
- `context`：当前任务、Workflow、市场、标的、可用工具。
- `volatile`：记忆、风险、图谱、工具结果摘要。

与 Hermes 不同的是，`finance-agent` 的 Prompt 需要面向金融推荐链路，所以还要显式增加：

- `tool_protocol`
- `risk_protocol`
- `market_rules`
- `output_schema`
- `reporting_constraints`

## 5. 核心对象设计

### 5.1 PromptTemplate

`PromptTemplate` 表示一份可渲染模板。

建议字段：

```python
PromptTemplate(
    template_id: str,
    version: str,
    model_role: str,
    market_scope: tuple[str, ...],
    workflow_scope: tuple[str, ...],
    sections: tuple[PromptTemplateSection, ...],
    output_schema: dict[str, Any],
)
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `template_id` | 模板唯一 ID，如 `primary_financial_analyst` |
| `version` | 代码内版本，如 `1.0.0` |
| `model_role` | 模型角色，如 `primary_financial_analyst` |
| `market_scope` | 适用市场，如 `ashare`、`crypto`、`*` |
| `workflow_scope` | 适用 Workflow，如 `asset_deep_analysis`、`*` |
| `sections` | 模板分段 |
| `output_schema` | 期望模型输出结构 |

### 5.2 PromptTemplateSection

`PromptTemplateSection` 表示模板的一段。

建议字段：

```python
PromptTemplateSection(
    name: str,
    content: str,
    required: bool = True,
)
```

推荐段落：

| section | 用途 |
| --- | --- |
| `identity` | 角色身份 |
| `boundaries` | 安全边界 |
| `act_guidance` | 先行动再解释 |
| `tool_protocol` | 工具调用规则 |
| `market_rules` | A 股 / 数字货币差异化规则 |
| `risk_protocol` | 风险反驳规则 |
| `output_schema` | 输出 JSON Schema |
| `reporting_constraints` | 中文报告约束 |

### 5.3 PromptBundle

`PromptBundle` 是实际传给模型调用链的渲染结果。

建议字段：

```python
PromptBundle(
    template_id: str,
    template_version: str,
    model_role: str,
    role_name: str | None,
    market_type: str,
    workflow_type: str,
    stable: str,
    context: str,
    volatile: str,
    role: str,
    output_schema: dict[str, Any],
)
```

当前可以继续返回 `dict[str, Any]`，但字段要向上面的结构对齐。

### 5.4 PromptRenderContext

`PromptRenderContext` 表示渲染上下文。

来源包括：

- `context_envelope`
- `model_role`
- `role_name`
- `market_type`
- `workflow_type`
- `available_tools`
- `role_view`
- `memory_summary`
- `risk_summary`

## 6. 模板注册表

第一阶段采用代码内注册表：

```python
PROMPT_TEMPLATE_REGISTRY = {
    "top_level_dispatcher": PromptTemplate(...),
    "primary_financial_analyst": PromptTemplate(...),
    "high_risk_reviewer": PromptTemplate(...),
    "workflow_role": PromptTemplate(...),
}
```

解析规则：

1. 按 `model_role` 找模板。
2. 按 `market_type` 检查适用市场。
3. 按 `workflow_type` 检查适用 Workflow。
4. 如果 `role_name` 存在，追加角色段。
5. 渲染为 `PromptBundle`。

## 7. 模板类型

### 7.1 top_level_dispatcher

用途：顶部金融 Agent 规划工具、Workflow 和图谱调用。

必须包含：

- 只使用已入库事实。
- 先调用工具或 Workflow，再输出结论。
- 不直接执行真实交易。
- A 股和数字货币必须分链路。
- 输出必须可审计。

### 7.2 primary_financial_analyst

用途：主分析模型，默认由 DeepSeek V4 Pro 承担。

必须包含：

- 汇总技术面、因子、风险、组合和记忆。
- 生成推荐解释和中文报告草稿。
- 不替代高风险复核。
- 遇到数据不足输出 `need_more_data`。

### 7.3 high_risk_reviewer

用途：高风险复核模型，默认由 GPT-5.5 Pro 承担。

必须包含：

- 对卖出、换股、换币、大仓位调整进行反驳。
- 对数据缺口、信号冲突、风险升高进行降级。
- 不能重新生成完整推荐链路。
- 输出复核结论：`approve`、`downgrade`、`reject`、`need_more_data`。

### 7.4 workflow_role

用途：Workflow 圆桌角色。

角色包括：

- `technical_analyst`
- `factor_analyst`
- `risk_rebuttal`
- `portfolio_manager`
- `memory_manager`

角色段只描述职责、可见字段和输出要求，不重复顶部安全边界。

## 8. 工具调用协议

所有模板都必须包含工具纪律：

1. 不允许编造行情、财务、因子、风险或记忆。
2. 需要事实时必须使用 `FinanceToolRuntime` 暴露的只读工具。
3. 工具返回缺失时，输出 `need_more_data`，不能用常识补齐。
4. 记忆只能作为历史参考，不能替代事实库。
5. 图谱路径只能作为原因链辅助，不能单独作为买卖依据。
6. 任何真实交易动作继续禁用，只能生成建议和人工确认任务。

## 9. 输出 JSON Schema

模型输出必须是 JSON 兼容结构。字段名使用英文，字段内容以中文为主。

基础 schema：

```json
{
  "summary_zh": "中文结论",
  "action": "watch|buy_candidate|avoid|sell_review|swap_review|need_more_data",
  "confidence": 0.0,
  "evidence_ids": [],
  "risk_flags": [],
  "tool_requests": [],
  "reasoning_brief_zh": "简短中文理由",
  "report_sections": {
    "technical": "",
    "factor": "",
    "risk_rebuttal": "",
    "portfolio": "",
    "memory": ""
  }
}
```

字段约束：

| 字段 | 约束 |
| --- | --- |
| `summary_zh` | 必填，中文 |
| `action` | 必填，只能取枚举值 |
| `confidence` | 必填，0 到 1 |
| `evidence_ids` | 必填，可为空 |
| `risk_flags` | 必填，可为空 |
| `tool_requests` | 可为空，用于声明还需要哪些工具 |
| `reasoning_brief_zh` | 必填，简短中文说明 |
| `report_sections` | 面向报告生成，可分段为空 |

高风险复核 schema：

```json
{
  "review_status": "approve|downgrade|reject|need_more_data",
  "summary_zh": "中文复核结论",
  "blocking_risks": [],
  "missing_evidence": [],
  "required_follow_up_tools": [],
  "confidence": 0.0
}
```

## 10. 风险反驳协议

风险反驳必须遵守：

1. 高风险动作默认先反驳，不默认通过。
2. 数据缺失、过期、来源不一致时，必须降级。
3. 技术信号和因子评分冲突时，必须说明冲突。
4. A 股出现停牌、退市、重大公告、质押、限售解禁等风险时，不能输出强买入。
5. 数字货币出现资金费率异常、清算风险、交易所流动性不足、稳定币风险时，不能输出强买入。
6. 记忆中的历史失败案例必须进入风险摘要，但不能单独否决。

## 11. 市场差异化规则

### 11.1 A 股规则

A 股模板需要关注：

- 交易日和停复牌。
- 涨跌停限制。
- 退市和 ST 风险。
- 财报、业绩预告、分红。
- 北向资金、主力资金、龙虎榜。
- 行业、概念、指数成分。

### 11.2 数字货币规则

数字货币模板需要关注：

- 24/7 连续行情。
- 交易所和交易对。
- 资金费率。
- 合约持仓和爆仓风险。
- 稳定币和流动性风险。
- 链上或交易所事件。

## 12. 中英文输出约束

规则：

- JSON 字段名使用英文。
- 面向用户的结论、理由、报告正文使用中文。
- 工具名、模型名、Workflow 类型、字段名保留英文。
- 不输出内部推理长链，只输出 `reasoning_brief_zh`。
- 不使用“保证收益”“稳赚”等表达。
- 不把建议表述为真实交易指令。

## 13. 渲染流程

推荐流程：

```text
context_envelope
  -> PromptRenderContext
  -> select PromptTemplate
  -> render stable sections
  -> render context prompt
  -> render volatile prompt
  -> render role prompt
  -> attach output_schema
  -> PromptBundle
```

`build_prompt_bundle()` 继续作为唯一入口。

## 14. 审计预留

本阶段不建表，但 `PromptBundle` 需要预留以下字段：

- `template_id`
- `template_version`
- `template_hash`
- `model_role`
- `role_name`
- `market_type`
- `workflow_type`

其中 `template_hash` 可先由模板内容计算，不需要数据库。

后续如果新增数据库审计，可以把这些字段写入：

- `prompt_templates`
- `model_prompt_runs`

## 15. 实现边界

第一阶段实现只修改：

- `src/finance_agent/agents/runtime/prompts.py`
- `scripts/storage/smoke_workflow_context_envelope.py`
- `scripts/storage/smoke_model_planner_scheduler_memory.py`

如有必要，可少量调整：

- `src/finance_agent/agents/loop/planner.py`
- `src/finance_agent/agents/runtime/langgraph_adapter.py`

不修改数据库 migration。

## 16. 验收标准

实现完成后必须满足：

1. `build_prompt_bundle()` 返回模板元信息。
2. Prompt Bundle 包含 `template_id`、`template_version`、`market_type`、`workflow_type`。
3. A 股和数字货币能渲染不同 `market_rules`。
4. 高风险复核模板包含反驳协议和复核 schema。
5. 主分析模板包含工具协议和推荐输出 schema。
6. 现有 planner 仍能读取 `model_prompt_bundle`。
7. 现有 smoke 全部通过。

## 17. 后续路线

结构化模板稳定后，再进入下一阶段：

1. 增加 `template_hash` 审计。
2. 把模板元信息写入 Workflow 审计。
3. 接真实模型调用。
4. 解析模型 JSON 输出并回写推荐、复核和报告。
5. 再评估是否需要数据库级 Prompt 模板表。
