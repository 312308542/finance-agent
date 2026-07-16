"""历史 A 股关键词新闻实体重验服务。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from finance_agent.data.news_entity_validation import (
    NEWS_ENTITY_RULE_VERSION,
    NewsEntityDecision,
    validate_ashare_news_entity,
)

JsonDict = dict[str, Any]


class NewsEntityRevalidationRepository(Protocol):
    """历史实体重验所需的最小仓储协议。"""

    def list_stock_news_for_entity_revalidation(
        self,
        *,
        limit: int | None = None,
    ) -> list[tuple[Any, str | None]]: ...

    def update_news_entity_validations(
        self,
        rows: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int: ...

    def update_evidence_entity_validations(
        self,
        rows: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int: ...


@dataclass(frozen=True)
class StockNewsEntityRevalidationResult:
    """单次历史新闻实体重验摘要。"""

    apply: bool
    scanned: int
    passed: int
    failed: int
    ambiguous: int
    missing_asset: int
    updated: int
    updated_events: int
    updated_evidence: int
    reason_counts: dict[str, int]

    def as_dict(self) -> JsonDict:
        """转换为命令行可直接输出的 JSON 对象。"""

        return asdict(self)


class StockNewsEntityRevalidationService:
    """确定性重验历史关键词新闻，并按需回填事件与证据。"""

    def __init__(self, repository: NewsEntityRevalidationRepository) -> None:
        self.repository = repository

    def run(
        self,
        *,
        apply: bool,
        limit: int | None = None,
        chunk_size: int = 500,
    ) -> StockNewsEntityRevalidationResult:
        """执行重验；只有显式 ``apply=True`` 才写入数据库。"""

        if limit is not None and limit <= 0:
            raise ValueError("limit 必须大于 0")
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")

        rows = self.repository.list_stock_news_for_entity_revalidation(limit=limit)
        revalidated_at = datetime.now(tz=UTC).isoformat()
        updates: list[JsonDict] = []
        status_counts = {"passed": 0, "failed": 0, "ambiguous": 0}
        reason_counts: dict[str, int] = {}
        missing_asset = 0

        for event, repository_asset_name in rows:
            asset_name = _resolve_asset_name(event, repository_asset_name)
            if not asset_name:
                missing_asset += 1
            decision = validate_ashare_news_entity(
                symbol=str(getattr(event, "symbol", "") or ""),
                asset_name=asset_name,
                title=getattr(event, "title", None),
                summary=getattr(event, "summary", None),
            )
            if decision.reason in {"missing_asset_name", "empty_text"}:
                decision = replace(decision, status="ambiguous")
            status_counts[decision.status] += 1
            reason_counts[decision.reason] = reason_counts.get(decision.reason, 0) + 1
            updates.append(
                {
                    "event_id": str(event.event_id),
                    "entity_validation": _decision_payload(
                        decision,
                        asset_name=asset_name,
                        revalidated_at=_existing_revalidated_at(event) or revalidated_at,
                    ),
                }
            )

        updated_events = 0
        updated_evidence = 0
        if apply and updates:
            updated_events = self.repository.update_news_entity_validations(
                updates,
                chunk_size=chunk_size,
            )
            updated_evidence = self.repository.update_evidence_entity_validations(
                updates,
                chunk_size=chunk_size,
            )

        return StockNewsEntityRevalidationResult(
            apply=apply,
            scanned=len(rows),
            passed=status_counts["passed"],
            failed=status_counts["failed"],
            ambiguous=status_counts["ambiguous"],
            missing_asset=missing_asset,
            updated=updated_events,
            updated_events=updated_events,
            updated_evidence=updated_evidence,
            reason_counts=dict(sorted(reason_counts.items())),
        )


def _resolve_asset_name(event: Any, repository_asset_name: str | None) -> str:
    asset_name = str(repository_asset_name or "").strip()
    if asset_name:
        return asset_name
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return ""
    validation = payload.get("entity_validation")
    if not isinstance(validation, dict):
        return ""
    return str(validation.get("asset_name") or "").strip()


def _decision_payload(
    decision: NewsEntityDecision,
    *,
    asset_name: str,
    revalidated_at: str,
) -> JsonDict:
    return {
        "status": decision.status,
        "reason": decision.reason,
        "matched_by": decision.matched_by,
        "expected_exchange": decision.expected_exchange,
        "asset_name": asset_name,
        "rule_version": NEWS_ENTITY_RULE_VERSION,
        "revalidated_at": revalidated_at,
    }


def _existing_revalidated_at(event: Any) -> str:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return ""
    validation = payload.get("entity_validation")
    if not isinstance(validation, dict):
        return ""
    return str(validation.get("revalidated_at") or "").strip()
