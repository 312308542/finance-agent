"""圆桌 Workflow 的真实模型观点节点。

本模块只负责单个圆桌角色的模型调用、结构化校验和 fallback 包装，不直接改变
Workflow 编排，也不写数据库。调用方可以在现有规则版观点外包一层降级策略。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from finance_agent.agents.runtime.model_client import ModelClient, ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig
from finance_agent.agents.workflows.roundtable_prompts import (
    OUTPUT_SCHEMA_PROMPT,
    role_prompt,
)

JsonDict = dict[str, Any]
MODEL_STANCES = {"bullish", "bearish", "neutral", "conflicted"}
DEFAULT_SCHEMA_FAILURE_GAP = "圆桌模型连续两次没有返回合法 JSON，已使用规则版观点。"


@dataclass(frozen=True)
class RoundtableOpinionRequest:
    """单个圆桌角色生成模型观点所需的上下文。"""

    role: str
    asset_id: str
    workflow_type: str
    context: JsonDict
    question: str
    allowed_evidence_ids: list[str] | None = None
    fallback_opinion: JsonDict | None = None


RoundtableStance = Literal["bullish", "bearish", "neutral", "conflicted"]


@dataclass(frozen=True)
class RoundtableOpinion:
    """经过校验后的模型圆桌观点。"""

    role: str
    asset_id: str
    stance: RoundtableStance
    confidence: float
    summary: str
    key_points: list[str]
    rebuttals: list[str]
    evidence_ids: list[str]
    data_gaps: list[str]
    generated_by: str
    model_instance_id: str | None = None

    def to_dict(self) -> JsonDict:
        """转换为 Workflow payload 可直接保存的字典。"""

        return {
            "role": self.role,
            "asset_id": self.asset_id,
            "stance": self.stance,
            "confidence": self.confidence,
            "summary": self.summary,
            "key_points": self.key_points,
            "rebuttals": self.rebuttals,
            "evidence_ids": self.evidence_ids,
            "data_gaps": self.data_gaps,
            "generated_by": self.generated_by,
            "model_instance_id": self.model_instance_id,
        }


def generate_model_opinion(
    *,
    request: RoundtableOpinionRequest,
    model_client: ModelClient,
    model_config: ModelEndpointConfig,
) -> JsonDict:
    """调用模型生成单个角色观点；失败时返回规则版 fallback。"""

    messages = build_roundtable_messages(request=request)
    try:
        response = model_client.invoke_json(
            config=model_config,
            messages=messages,
            temperature=0.0,
        )
        opinion = normalize_roundtable_payload(
            payload=response.parsed_json,
            request=request,
            model_config=model_config,
            response=response,
        )
        if opinion is None:
            retry_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "上次输出不是合法 JSON，或字段不符合圆桌观点协议。"
                        "请只输出包含 role/stance/confidence/summary/key_points/"
                        "rebuttals/evidence_ids/data_gaps 的 JSON。"
                    ),
                },
            ]
            response = model_client.invoke_json(
                config=model_config,
                messages=retry_messages,
                temperature=0.0,
            )
            opinion = normalize_roundtable_payload(
                payload=response.parsed_json,
                request=request,
                model_config=model_config,
                response=response,
            )
        if opinion is None:
            return build_fallback_opinion(
                request=request,
                data_gaps=[DEFAULT_SCHEMA_FAILURE_GAP],
            )
        return opinion
    except Exception as exc:  # noqa: BLE001 - 圆桌模型必须可降级
        return build_fallback_opinion(request=request, model_error=str(exc))


def build_roundtable_messages(*, request: RoundtableOpinionRequest) -> list[JsonDict]:
    """构造圆桌模型调用消息。"""

    context_text = json.dumps(request.context, ensure_ascii=False, default=str)
    allowed_ids = request.allowed_evidence_ids or collect_evidence_ids(request.context)
    return [
        {
            "role": "system",
            "content": (
                f"{role_prompt(request.role)}\n\n"
                f"{OUTPUT_SCHEMA_PROMPT}\n\n"
                "你只能引用 allowed_evidence_ids 中的证据 ID。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"workflow_type: {request.workflow_type}\n"
                f"asset_id: {request.asset_id}\n"
                f"role: {request.role}\n"
                f"allowed_evidence_ids: {allowed_ids}\n"
                f"question: {request.question}\n"
                f"context_json: {context_text}"
            ),
        },
    ]


def normalize_roundtable_payload(
    *,
    payload: JsonDict | None,
    request: RoundtableOpinionRequest,
    model_config: ModelEndpointConfig,
    response: ModelClientResponse,
) -> JsonDict | None:
    """严格校验模型 JSON，并过滤虚构 evidence。"""

    if not isinstance(payload, dict):
        return None
    role = str(payload.get("role") or request.role)
    if role != request.role:
        return None
    stance = str(payload.get("stance") or "")
    if stance not in MODEL_STANCES:
        return None
    confidence = normalize_confidence(payload.get("confidence"))
    if confidence is None:
        return None
    summary = payload.get("summary")
    key_points = normalize_string_list(payload.get("key_points"))
    rebuttals = normalize_string_list(payload.get("rebuttals"))
    evidence_ids = normalize_string_list(payload.get("evidence_ids"))
    data_gaps = normalize_string_list(payload.get("data_gaps"))
    if (
        not isinstance(summary, str)
        or key_points is None
        or rebuttals is None
        or evidence_ids is None
        or data_gaps is None
    ):
        return None
    evidence_ids, data_gaps = filter_allowed_evidence_ids(
        evidence_ids=evidence_ids,
        data_gaps=data_gaps,
        allowed_evidence_ids=request.allowed_evidence_ids
        or collect_evidence_ids(request.context),
    )
    return {
        "role": role,
        "asset_id": request.asset_id,
        "stance": stance,
        "confidence": confidence,
        "summary": summary,
        "key_points": key_points,
        "rebuttals": rebuttals,
        "evidence_ids": evidence_ids,
        "data_gaps": data_gaps,
        "generated_by": "model",
        "model_instance_id": model_config.model_key,
        "model": response.to_audit_dict(),
    }


def build_fallback_opinion(
    *,
    request: RoundtableOpinionRequest,
    model_error: str | None = None,
    data_gaps: list[str] | None = None,
) -> JsonDict:
    """把规则版观点包装成统一的 fallback 结构。"""

    base = dict(request.fallback_opinion or {})
    base.setdefault("role", request.role)
    base.setdefault("asset_id", request.asset_id)
    base.setdefault("stance", "neutral")
    base.setdefault("summary", "圆桌模型不可用，已使用规则版观点。")
    base.setdefault("key_points", [])
    base.setdefault("rebuttals", [])
    base.setdefault("evidence_ids", [])
    base["generated_by"] = "fallback"
    base["model_instance_id"] = None
    if data_gaps is not None:
        base["data_gaps"] = data_gaps
    else:
        base.setdefault("data_gaps", [])
    if model_error:
        base["model_error"] = model_error
    return base


def filter_allowed_evidence_ids(
    *,
    evidence_ids: list[str],
    data_gaps: list[str],
    allowed_evidence_ids: list[str],
) -> tuple[list[str], list[str]]:
    """移除模型凭空生成的 evidence_id，并写入数据缺口说明。"""

    allowed = set(allowed_evidence_ids)
    filtered: list[str] = []
    removed: list[str] = []
    for evidence_id in evidence_ids:
        if evidence_id in allowed and evidence_id not in filtered:
            filtered.append(evidence_id)
        elif evidence_id not in allowed:
            removed.append(evidence_id)
    if removed:
        data_gaps = [
            *data_gaps,
            f"模型引用了未出现在上下文中的 evidence_id，已剔除：{', '.join(removed)}",
        ]
    return filtered, data_gaps


def collect_evidence_ids(value: object) -> list[str]:
    """从嵌套上下文中收集 evidence_id 白名单。"""

    result: list[str] = []
    if isinstance(value, dict):
        evidence_id = value.get("evidence_id")
        if isinstance(evidence_id, str) and evidence_id not in result:
            result.append(evidence_id)
        for child in value.values():
            for child_id in collect_evidence_ids(child):
                if child_id not in result:
                    result.append(child_id)
    elif isinstance(value, list):
        for child in value:
            for child_id in collect_evidence_ids(child):
                if child_id not in result:
                    result.append(child_id)
    return result


def normalize_confidence(value: object) -> float | None:
    """把置信度清洗为 0~1 浮点数。"""

    if not isinstance(value, int | float | str):
        return None
    try:
        confidence = float(value)
    except ValueError:
        return None
    if confidence < 0 or confidence > 1:
        return None
    return confidence


def normalize_string_list(value: object) -> list[str] | None:
    """校验并清洗字符串列表。"""

    if value is None:
        return []
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return value
