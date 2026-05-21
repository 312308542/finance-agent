"""Agent 与 LangGraph Workflow 适配层。"""

from finance_agent.agents.runtime.context_envelope import (
    CONTEXT_ENVELOPE_VERSION,
    ContextEnvelope,
    RoleView,
    build_workflow_context_envelope,
)
from finance_agent.agents.runtime.langgraph_adapter import (
    LangGraphWorkflowAdapter,
    WorkflowNodeEvent,
    extract_context_envelope,
    summarize_context_envelope,
    summarize_model_prompt_bundle,
)
from finance_agent.agents.runtime.model_client import (
    ModelClient,
    ModelClientResponse,
    OpenAICompatibleModelClient,
    extract_json_object,
)
from finance_agent.agents.runtime.model_config import (
    ModelEndpointConfig,
    ModelRegistry,
    attach_config_status,
    load_model_registry,
    preview_model_routes,
    test_model_endpoint,
)
from finance_agent.agents.runtime.model_router import ModelRoute, ModelRoutingPolicy
from finance_agent.agents.runtime.policies import HighRiskReviewPolicy, ReviewDecisionContext
from finance_agent.agents.runtime.prompts import (
    HIGH_RISK_REVIEW_STABLE_PROMPT,
    PRIMARY_ANALYST_STABLE_PROMPT,
    ROLE_PROMPTS,
    TOP_LEVEL_STABLE_PROMPT,
    build_context_prompt,
    build_prompt_bundle,
    build_volatile_prompt,
    resolve_stable_prompt,
)

__all__ = [
    "HighRiskReviewPolicy",
    "CONTEXT_ENVELOPE_VERSION",
    "ContextEnvelope",
    "LangGraphWorkflowAdapter",
    "ModelClient",
    "ModelClientResponse",
    "ModelEndpointConfig",
    "ModelRegistry",
    "ModelRoute",
    "ModelRoutingPolicy",
    "OpenAICompatibleModelClient",
    "ReviewDecisionContext",
    "RoleView",
    "WorkflowNodeEvent",
    "HIGH_RISK_REVIEW_STABLE_PROMPT",
    "PRIMARY_ANALYST_STABLE_PROMPT",
    "ROLE_PROMPTS",
    "TOP_LEVEL_STABLE_PROMPT",
    "attach_config_status",
    "build_context_prompt",
    "build_prompt_bundle",
    "load_model_registry",
    "preview_model_routes",
    "build_workflow_context_envelope",
    "build_volatile_prompt",
    "extract_context_envelope",
    "extract_json_object",
    "resolve_stable_prompt",
    "summarize_context_envelope",
    "summarize_model_prompt_bundle",
    "test_model_endpoint",
]
