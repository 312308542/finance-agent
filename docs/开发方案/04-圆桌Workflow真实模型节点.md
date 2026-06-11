# 方案 04：圆桌 Workflow 真实模型节点（O4 收尾）

> 优先级：P0 ｜ 批次 2 ｜ 依赖：方案 03（复用模型调用与 JSON 校验骨架 `review_protocol.py`）
> 前置阅读：`00-总体规划与执行约定.md`

## 1. 背景与现状

已有资产：

- `src/finance_agent/agents/workflows/`：`portfolio_monitoring.py`、`recommendation_decision.py`、`watchlist_management.py` + `langgraph_graphs.py`（LangGraph 编排），加上报告类三个 Workflow（asset_deep_analysis / swap_decision / daily_review）共 6 个入口，统一由 `FinanceAssistantService`（`src/finance_agent/agents/personal_assistant.py`）调度。
- 每个 Workflow 已输出：`node_trace`、`tool_calls`、`roundtable_opinions`（技术分析、因子分析、风险反驳、组合经理、记忆管理员五个角色）、`model_routes`、`high_risk_reviews`、完整中文报告（`build_chinese_decision_report`）。
- **但圆桌观点是规则版**：五个角色的观点由确定性规则从工具上下文拼装，不经过模型。
- 真实模型调用能力已存在：内部 Agent Loop 的 `ModelFinanceAgentPlanner`（`agents/loop/planner.py`）实现了"模型→白名单工具→观察→模型"受控循环；CLI 聊天也有模型工具循环。模式可直接借鉴。
- 工具层上下文完备：`FinanceToolRuntime` 的 `factor.get_asset_factor_context`、`signal_risk.get_asset_context`、`portfolio.get_snapshot`、`watchlist.get_active_items`、`memory.recall_asset_memories` 已被各 Workflow 调用。

**缺口**：圆桌的"分析师"没有真的分析。规则版观点是模板句，无法解释信号冲突、不能产生有信息量的反方观点——这是"中文可解释"目标的核心短板。

## 2. 目标与非目标

**目标**

1. 五个圆桌角色的观点由真实模型生成（结构化 JSON），规则版降级为 fallback 并完整保留。
2. 模型观点严格只读：引用工具上下文中的事实和 evidence_ids，不得产生新数字结论。
3. 每个角色观点带 `generated_by`（model/fallback）标记，审计与报告可区分。
4. 用户反馈 API 落地（消费方案 03 的 `needs_human` 与报告里的建议）。

**非目标**

- 不改变 Workflow 的节点编排、DTO、落库链路（圆桌节点内部替换实现，外部协议不变）。
- 不让模型修改裁决分数或动作枚举——最终裁决仍由组合经理节点按确定性规则综合（模型观点是输入之一）。
- 确认/执行闭环归方案 08。

## 3. 技术方案

### 3.1 圆桌角色模型化的统一骨架

新增 `src/finance_agent/agents/workflows/roundtable_model_nodes.py`：

```text
class RoundtableOpinionRequest:   # 角色名、资产上下文(工具层已组装)、关注问题
class RoundtableOpinion(BaseModel):  # pydantic 校验模型输出
    role: str
    stance: Literal["bullish","bearish","neutral","conflicted"]
    confidence: float            # 0~1，仅表达观点强度，不是评分
    key_points: list[str]        # 每条必须引用上下文中的具体事实
    rebuttals: list[str]         # 反方观点/失败条件
    evidence_ids: list[str]      # 只允许引用上下文中出现过的 evidence_id
    data_gaps: list[str]

def generate_model_opinion(role, context, model_client, routing) -> RoundtableOpinion | None
```

- 复用方案 03 的 `review_protocol.py` JSON 校验骨架（严格解析、一次纠错重试、失败返回 None）。
- `generate_model_opinion` 返回 None 时调用方落回规则版观点，并打 `generated_by="fallback"`。
- **evidence_ids 白名单校验**：解析后逐个核对模型引用的 evidence_id 是否真的存在于输入上下文，凭空引用的剔除并记入 `data_gaps`——这是防幻觉的硬约束。

### 3.2 五个角色的 prompt 模板

新增 `src/finance_agent/agents/workflows/roundtable_prompts.py`，每个角色一个模板常量，共同结构：

1. 角色定位（与 `docs/项目计划.md` 9.3 节六类分析师对齐）。
2. 输入上下文（序列化的工具层 JSON，字段保持中文注释）。
3. 输出 schema 与禁令："不得给出目标价；不得修改任何分数；不得引用上下文之外的数据；面向金融新手解释术语"。
4. 风险反驳角色额外强化："你的职责是挑毛病，必须输出至少 2 条反方观点，找不到就说明数据不足以反驳，不许敷衍"。

### 3.3 接入点与路由

- 在 `langgraph_graphs.py` 的圆桌节点中：先尝试 `generate_model_opinion`（路由走 `ModelRoutingPolicy` 常规分析路由），None 则用现有规则版函数。**只在节点内部替换实现，节点的输入输出签名不动**——这样 6 个 Workflow 一次性全部受益。
- 并发与成本控制：单次 Workflow 最多 5 次模型调用（每角色一次）；增加任务级参数 `roundtable_model_roles`（默认全部，可收缩为 `["risk_rebuttal","portfolio_manager"]` 降成本）。
- 观点落库：`roundtable_opinions` 既有结构增加 `generated_by`、`model_instance_id` 字段（payload 内，不动表结构）。

### 3.4 报告整合

- `build_chinese_decision_report` 的圆桌观点 section 改为渲染模型观点的 `key_points` / `rebuttals`（规则版字段兼容）。改模板前跑 gitnexus_impact——该函数被 6 个 Workflow 和多个 smoke 引用。

### 3.5 用户反馈 API

- 新增路由（`src/finance_agent/api/routes.py`）：
  - `POST /api/decisions/{decision_id}/feedback`，body：`{"feedback": "accepted|rejected|modified|deferred", "comment": "...", "modified_action": "..."}`。
  - 落到既有 `MemoryService.record_user_feedback`（已实现决策日志写入、反馈记忆、置信度调整）。
  - `GET /api/decisions/pending-confirmation`：列出 `pending_user_confirmation` 状态的决策（方案 03 产出 + 高风险建议），供方案 07 前端使用。
- schemas 对应增加 pydantic 模型。

## 4. 任务拆解

- [ ] T1 TDD：`tests/test_roundtable_model_nodes.py`——fake model client，覆盖：合法观点、非法 JSON 纠错重试、evidence 凭空引用被剔除、模型不可用 fallback、`generated_by` 标记。
- [ ] T2 实现 `roundtable_model_nodes.py` + `roundtable_prompts.py`。
- [ ] T3 逐 Workflow 接入（顺序：recommendation_decision → portfolio_monitoring → watchlist_management → 三个报告类；每接一个跑该 Workflow 的既有测试与 smoke，确认规则版回归不破）。
- [ ] T4 报告模板渲染模型观点（gitnexus_impact 先行）。
- [ ] T5 用户反馈 API + `tests/test_decision_feedback_api.py`。
- [ ] T6 真实联调：配置真实常规分析模型跑一次 `asset_deep_analysis`，人工审阅观点质量（重点：风险反驳是否言之有物），样例存本文档附录（脱敏）。
- [ ] T7 文档同步：`docs/优化版本进度跟踪表.md` O4 置为"真实模型版已完成"。

## 5. 验收命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_roundtable_model_nodes.py tests/test_decision_feedback_api.py -q
.\.venv\Scripts\python.exe scripts\storage\smoke_portfolio_watchlist_roundtable_workflows.py
.\.venv\Scripts\python.exe scripts\storage\smoke_roundtable_report_workflows.py
.\.venv\Scripts\python.exe -m pytest -q
```

验收标准：

- 模型可用时，6 个 Workflow 的圆桌观点 `generated_by="model"` 且每条 key_point 可追溯到工具上下文。
- 拔掉模型配置重跑，全部 Workflow 仍成功完成（fallback），报告标注规则版。
- 既有全部 smoke 不破。

## 6. 风险与注意事项

- **成本**：6 Workflow × 5 角色全开模型在批量场景下调用量大。`roundtable_model_roles` 参数必须做，且 daily_review 这类批量 Workflow 默认只开风险反驳 + 组合经理两个角色。
- **延迟**：圆桌节点串行调模型会显著拉长 Workflow 时长；五个角色观点彼此独立，可在节点内并发（线程池，上限 3），但要保证 LangGraph 状态写入仍是单线程汇总。
- 规则版函数**不许删除**——它是模型断供时的生命线，也是测试基线。
- prompt 里序列化上下文注意截断策略：上下文超长时优先保留信号/风险/评分摘要，截断原始明细，并在 `data_gaps` 注明截断。

## 7. 进度表

| 任务 | 状态 | 验证记录 |
| --- | --- | --- |
| T1 节点测试 | 未开始 | - |
| T2 骨架与 prompt | 未开始 | - |
| T3 六个 Workflow 接入 | 未开始 | - |
| T4 报告渲染 | 未开始 | - |
| T5 反馈 API | 未开始 | - |
| T6 真实联调 | 未开始 | - |
| T7 文档同步 | 未开始 | - |
