"""回测命令和调度入口的数据库编排层。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_agent.backtesting.models import BacktestResult, JsonDict
from finance_agent.backtesting.service import BacktestService, BacktestServiceRequest, ScoreMode
from finance_agent.storage.orm import AssetScoreORM, ScreeningResultORM
from finance_agent.storage.repositories import BacktestRepository, MarketDataRepository


class DatabaseBacktestScoreSource:
    """从已落库评分表读取回测所需评分截面。"""

    def __init__(
        self,
        session: Session,
        *,
        market: str,
        horizon: str = "swing",
        max_rows: int = 1000,
    ) -> None:
        self.session = session
        self.market = market
        self.horizon = horizon
        self.max_rows = max(max_rows, 1)

    def list_replayed_scores(
        self,
        *,
        universe_id: str,
        strategy_id: str,
        as_of: datetime,
    ) -> list[AssetScoreORM]:
        """读取最新评分作为当前权重回放的近似截面。"""

        return self._list_scores(
            universe_id=universe_id,
            strategy_id=strategy_id,
            as_of=as_of,
            latest=True,
        )

    def list_historical_scores(
        self,
        *,
        universe_id: str,
        strategy_id: str,
        as_of: datetime,
    ) -> list[AssetScoreORM]:
        """读取回测起点之前已存在的历史评分截面。"""

        return self._list_scores(
            universe_id=universe_id,
            strategy_id=strategy_id,
            as_of=as_of,
            latest=False,
        )

    def _list_scores(
        self,
        *,
        universe_id: str,
        strategy_id: str,
        as_of: datetime,
        latest: bool,
    ) -> list[AssetScoreORM]:
        latest_statement = (
            select(
                AssetScoreORM.score_id.label("score_id"),
                func.row_number()
                .over(
                    partition_by=AssetScoreORM.asset_id,
                    order_by=(
                        ScreeningResultORM.as_of.desc(),
                        AssetScoreORM.as_of.desc(),
                        AssetScoreORM.rank,
                    ),
                )
                .label("latest_rank"),
            )
            .join(
                ScreeningResultORM,
                ScreeningResultORM.screening_id == AssetScoreORM.screening_id,
            )
            .where(
                AssetScoreORM.market == self.market,
                AssetScoreORM.universe_id == universe_id,
                AssetScoreORM.horizon == self.horizon,
                AssetScoreORM.strategy_id == strategy_id,
                AssetScoreORM.status.in_(("available", "partial")),
            )
        )
        if not latest:
            latest_statement = latest_statement.where(
                AssetScoreORM.as_of <= as_of,
                ScreeningResultORM.as_of <= as_of,
            )
        latest_scores = latest_statement.subquery("asset_score_latest")
        statement = (
            select(AssetScoreORM)
            .join(
                latest_scores,
                latest_scores.c.score_id == AssetScoreORM.score_id,
            )
            .where(latest_scores.c.latest_rank == 1)
            .order_by(
                AssetScoreORM.total_score.desc(),
                AssetScoreORM.rank,
                AssetScoreORM.asset_id,
            )
            .limit(self.max_rows)
        )
        rows = list(
            self.session.scalars(statement)
        )
        return _dedupe_latest_scores(rows)


class DatabaseBacktestPriceSource:
    """从标准日 K 表读取回测价格序列。"""

    def __init__(
        self,
        session: Session,
        *,
        timeframe: str = "1d",
        source: str | None = None,
    ) -> None:
        self.repository = MarketDataRepository(session)
        self.timeframe = timeframe
        self.source = source

    def list_bars(self, *, asset_id: str, start_at: datetime, end_at: datetime) -> list[Any]:
        """读取单资产指定窗口内的 K 线。"""

        return self.repository.list_window_bars(
            asset_ids=[asset_id],
            timeframe=self.timeframe,
            start_at=start_at,
            end_at=end_at,
            source=self.source,
        )


def run_factor_score_topn_backtest(
    session: Session,
    *,
    strategy: str,
    market: str,
    universe_id: str,
    strategy_id: str,
    years: int = 5,
    score_mode: ScoreMode = "replayed",
    topn: int = 20,
    rebalance: str = "once",
    timeframe: str = "1d",
    horizon: str = "swing",
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    price_source: str | None = None,
) -> JsonDict:
    """运行内置 TopN 评分策略回测并保存结果。"""

    if strategy != "factor_score_topn":
        raise ValueError("当前仅支持 factor_score_topn 回测策略")
    normalized_years = max(int(years), 1)
    normalized_end_at = _ensure_aware(end_at or datetime.now(tz=UTC))
    normalized_start_at = _ensure_aware(
        start_at or (normalized_end_at - timedelta(days=365 * normalized_years))
    )
    request = BacktestServiceRequest(
        market=market,
        universe_id=universe_id,
        strategy_id=strategy_id,
        start_at=normalized_start_at,
        end_at=normalized_end_at,
        score_mode=score_mode,
        topn=topn,
        rebalance=rebalance,
        timeframe=timeframe,
    )
    result = BacktestService(
        score_source=DatabaseBacktestScoreSource(session, market=market, horizon=horizon),
        price_source=DatabaseBacktestPriceSource(
            session,
            timeframe=timeframe,
            source=price_source,
        ),
    ).run_topn(request)
    backtest_id = build_backtest_id(
        market=market,
        strategy=strategy,
        universe_id=universe_id,
        strategy_id=strategy_id,
        start_at=normalized_start_at,
        end_at=normalized_end_at,
        score_mode=score_mode,
        topn=topn,
    )
    payload = result.to_dict()
    row = BacktestRepository(session).upsert_result(
        backtest_id=backtest_id,
        market=market,
        strategy_id=strategy_id,
        universe_id=universe_id,
        start_at=normalized_start_at,
        end_at=normalized_end_at,
        rebalance_frequency=rebalance,
        metrics=result.metrics,
        data_versions=result.data_versions,
        status=result.status,
        payload=payload | {
            "strategy": strategy,
            "horizon": horizon,
            "years": normalized_years,
            "price_source": price_source,
        },
    )
    return {
        "status": result.status,
        "backtest_id": row.backtest_id,
        "market": market,
        "strategy": strategy,
        "strategy_id": strategy_id,
        "universe_id": universe_id,
        "start_at": normalized_start_at.isoformat(),
        "end_at": normalized_end_at.isoformat(),
        "metrics": result.metrics,
        "data_versions": result.data_versions,
        "warnings": list(result.warnings),
        "payload": payload,
    }


def build_backtest_id(
    *,
    market: str,
    strategy: str,
    universe_id: str,
    strategy_id: str,
    start_at: datetime,
    end_at: datetime,
    score_mode: str,
    topn: int,
) -> str:
    """生成稳定回测 ID，避免同一窗口重复写出多份记录。"""

    raw = "|".join(
        [
            market,
            strategy,
            universe_id,
            strategy_id,
            start_at.isoformat(),
            end_at.isoformat(),
            score_mode,
            str(topn),
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    return f"bt:{digest}"


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _dedupe_latest_scores(rows: list[AssetScoreORM]) -> list[AssetScoreORM]:
    deduped: list[AssetScoreORM] = []
    seen: set[str] = set()
    for row in rows:
        if row.asset_id in seen:
            continue
        seen.add(row.asset_id)
        deduped.append(row)
    return deduped
