from pathlib import Path
from typing import Any

from finance_agent.scoring.service import build_score_id
from finance_agent.storage.orm import AssetScoreORM
from finance_agent.storage.repositories import AssetScoreRepository


def test_asset_score_orm_has_required_strategy_dimension() -> None:
    """评分主表必须用非空物理列保存策略，而不是只依赖 payload。"""

    column = AssetScoreORM.__table__.columns["strategy_id"]

    assert column.nullable is False
    assert column.type.length == 128


def test_score_id_changes_when_strategy_changes() -> None:
    """同一因子帧的不同策略评分必须得到不同主键。"""

    common = {
        "universe_id": "universe:merged:ashare:recommendation",
        "asset_id": "ashare:000001",
        "horizon": "swing",
        "factor_frame_id": "factor:ashare:000001:swing:20260715T000000Z",
    }

    short_id = build_score_id(**common, strategy_id="strategy:ashare:short_swing")
    theme_id = build_score_id(**common, strategy_id="strategy:ashare:theme_momentum")

    assert short_id != theme_id
    assert short_id.endswith(":strategy:d88e767260b5")
    assert theme_id.endswith(":strategy:a07e85ad89cd")


def test_asset_score_strategy_migration_preserves_recommendation_references() -> None:
    """迁移必须回填策略、同步推荐引用并在最后加非空约束。"""

    migration = Path(
        "src/finance_agent/storage/migrations/versions/"
        "20260716_0021_add_asset_score_strategy.py"
    )
    content = migration.read_text(encoding="utf-8")

    assert 'revision = "20260716_0021"' in content
    assert 'down_revision = "20260630_0020"' in content
    assert 'op.add_column("asset_scores"' in content
    assert "strategy:ashare:legacy_default" in content
    assert "UPDATE asset_recommendations" in content
    assert "UPDATE asset_scores" in content
    assert 'nullable=False' in content
    assert "拒绝降级" in content
    assert "chr(58)" in content
    assert "|| ':legacy_default'" not in content


def test_asset_score_repository_filters_physical_strategy_dimension() -> None:
    """评分列表和最新评分查询必须使用物理策略列过滤。"""

    class _ScalarResult:
        def __iter__(self) -> Any:
            return iter(())

        def one_or_none(self) -> None:
            return None

    class _CapturingSession:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        def scalars(self, statement: Any) -> _ScalarResult:
            self.statements.append(statement)
            return _ScalarResult()

    session = _CapturingSession()
    repository = AssetScoreRepository(session)  # type: ignore[arg-type]

    repository.list_scores_for_screening(
        "screen:ashare",
        strategy_id="strategy:ashare:short_swing",
    )
    repository.get_latest_score(
        asset_id="ashare:000001",
        horizon="swing",
        strategy_id="strategy:ashare:short_swing",
    )

    sql = [
        str(statement.compile(compile_kwargs={"literal_binds": True}))
        for statement in session.statements
    ]
    assert len(sql) == 2
    assert all(
        "asset_scores.strategy_id = 'strategy:ashare:short_swing'" in statement
        for statement in sql
    )
