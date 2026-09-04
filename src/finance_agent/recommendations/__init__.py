"""推荐结果生成服务。"""

from finance_agent.recommendations.decision_snapshot import (
    DecisionFact,
    DecisionSnapshot,
    DecisionSnapshotBuilder,
    DecisionSnapshotBuildResult,
    DecisionSnapshotInputs,
)
from finance_agent.recommendations.service import RecommendationRunResult, RecommendationService

__all__ = [
    "DecisionFact",
    "DecisionSnapshot",
    "DecisionSnapshotBuildResult",
    "DecisionSnapshotBuilder",
    "DecisionSnapshotInputs",
    "RecommendationRunResult",
    "RecommendationService",
]
