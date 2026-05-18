"""供 Hermes 和 LangGraph Workflow 调用的金融事实工具运行时。

工具层只读取本项目已经清洗入库的数据，不直接调用 AKShare、Binance、ccxt
或网页接口。写入类动作仍通过业务服务和 Workflow 决策链路完成。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.application import PortfolioService, WatchlistService
from finance_agent.storage.orm import (
    AssetScoreORM,
    AssistantMemoryORM,
    DataQualitySnapshotORM,
    EvidenceORM,
    FactorFrameORM,
    IndicatorFrameORM,
    PortfolioORM,
    PositionORM,
    RecommendationRunORM,
    RiskFindingORM,
    SignalSnapshotORM,
    WatchlistItemORM,
)
from finance_agent.storage.repositories import (
    AssetScoreRepository,
    DataQualityRepository,
    EventRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
    MemoryRepository,
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)

JsonDict = dict[str, Any]
ToolHandler = Callable[..., JsonDict]


@dataclass(frozen=True)
class FinanceTool:
    """金融事实工具定义。"""

    name: str
    description: str
    handler: ToolHandler


class FinanceToolRuntime:
    """统一封装金融事实查询工具。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.portfolios = PortfolioService(session)
        self.watchlists = WatchlistService(session)
        self.recommendations = RecommendationRepository(session)
        self.indicators = IndicatorFrameRepository(session)
        self.factors = FactorFrameRepository(session)
        self.scores = AssetScoreRepository(session)
        self.events = EventRepository(session)
        self.signals = SignalSnapshotRepository(session)
        self.risks = RiskRepository(session)
        self.memories = MemoryRepository(session)
        self.data_quality = DataQualityRepository(session)
        self._tools: dict[str, FinanceTool] = {}
        self._register_builtin_tools()

    def list_tools(self) -> tuple[str, ...]:
        """列出可调用工具名称。"""

        return tuple(sorted(self._tools))

    def get_tool(self, name: str) -> FinanceTool:
        """读取工具定义。"""

        return self._tools[name]

    def call(self, name: str, **kwargs: Any) -> JsonDict:
        """调用工具并返回结构化结果。"""

        return self.get_tool(name).handler(**kwargs)

    def register(self, tool: FinanceTool) -> None:
        """注册工具。"""

        self._tools[tool.name] = tool

    def _register_builtin_tools(self) -> None:
        """注册第一批金融事实查询工具。"""

        self.register(
            FinanceTool(
                name="portfolio.get_snapshot",
                description="读取组合和当前活跃持仓快照。",
                handler=self.get_portfolio_snapshot,
            )
        )
        self.register(
            FinanceTool(
                name="watchlist.get_active_items",
                description="读取私人观察池活跃观察项。",
                handler=self.get_active_watchlist_items,
            )
        )
        self.register(
            FinanceTool(
                name="recommendation.get_run",
                description="读取一次推荐运行及其推荐结果。",
                handler=self.get_recommendation_run,
            )
        )
        self.register(
            FinanceTool(
                name="signal_risk.get_asset_context",
                description="读取单标的最新信号、风险和数据质量上下文。",
                handler=self.get_asset_signal_risk_context,
            )
        )
        self.register(
            FinanceTool(
                name="factor.get_asset_factor_context",
                description="读取单标的 TA 指标、因子、评分和证据上下文。",
                handler=self.get_asset_factor_context,
            )
        )
        self.register(
            FinanceTool(
                name="memory.recall_asset_memories",
                description="召回标的相关 Finance Memory。",
                handler=self.recall_asset_memories,
            )
        )
        self.register(
            FinanceTool(
                name="workflow.list_workflows",
                description="列出可由 Hermes 或业务服务调用的金融团队 Workflow。",
                handler=self.list_workflows,
            )
        )

    def get_portfolio_snapshot(self, *, portfolio_id: str) -> JsonDict:
        """读取组合和持仓快照。"""

        snapshot = self.portfolios.load_portfolio_snapshot(portfolio_id)
        return {
            "portfolio": serialize_portfolio(snapshot.portfolio),
            "positions": [serialize_position(position) for position in snapshot.positions],
        }

    def get_active_watchlist_items(
        self,
        *,
        owner_id: str,
        watchlist_id: str | None = None,
    ) -> JsonDict:
        """读取活跃观察项。"""

        items = self.watchlists.list_active_items(owner_id=owner_id, watchlist_id=watchlist_id)
        return {"items": [serialize_watchlist_item(item) for item in items]}

    def get_recommendation_run(
        self,
        *,
        run_id: str,
        limit: int = 20,
    ) -> JsonDict:
        """读取推荐运行和前 N 条推荐结果。"""

        run = self.session.get(RecommendationRunORM, run_id)
        if run is None:
            return {"run": None, "recommendations": []}
        recommendations = self.recommendations.list_top_recommendations(
            run_id=run_id,
            limit=limit,
        )
        return {
            "run": serialize_recommendation_run(run),
            "recommendations": [
                serialize_asset_recommendation(recommendation)
                for recommendation in recommendations
            ],
        }

    def get_asset_signal_risk_context(
        self,
        *,
        asset_id: str,
        horizon: str = "swing",
        risk_limit: int = 5,
        quality_limit: int = 5,
    ) -> JsonDict:
        """读取标的信号、风险和数据质量上下文。"""

        signal = self.signals.get_latest_signal(asset_id=asset_id, horizon=horizon)
        risks = self.risks.list_recent_risks(asset_id=asset_id, limit=risk_limit)
        qualities = self.data_quality.list_latest_quality(asset_id=asset_id, limit=quality_limit)
        return {
            "asset_id": asset_id,
            "horizon": horizon,
            "signal": serialize_signal(signal) if signal else None,
            "risks": [serialize_risk(risk) for risk in risks],
            "data_quality": [serialize_data_quality(item) for item in qualities],
        }

    def get_asset_factor_context(
        self,
        *,
        asset_id: str,
        horizon: str = "swing",
        timeframe: str = "1d",
        library: str | None = None,
        evidence_limit: int = 5,
    ) -> JsonDict:
        """读取单标的指标、因子、评分和证据上下文。

        该工具给圆桌角色使用。它只读取数据库中已经由 TA-Lib、pandas/numpy、
        AKShare、Binance/ccxt 等数据层产出的结构化事实。
        """

        indicator = self.indicators.get_latest_indicator_frame(
            asset_id=asset_id,
            timeframe=timeframe,
            horizon=horizon,
            library=library,
        )
        factor = self.factors.get_latest_factor_frame(asset_id=asset_id, horizon=horizon)
        score = find_latest_score(self.session, asset_id=asset_id, horizon=horizon)
        evidence = list_recent_evidence(self.session, asset_id=asset_id, limit=evidence_limit)
        return {
            "asset_id": asset_id,
            "horizon": horizon,
            "timeframe": timeframe,
            "indicator_frame": serialize_indicator_frame(indicator) if indicator else None,
            "factor_frame": serialize_factor_frame(factor) if factor else None,
            "score": serialize_asset_score(score) if score else None,
            "evidence": [serialize_evidence(item) for item in evidence],
        }

    def recall_asset_memories(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> JsonDict:
        """召回 Finance Memory。"""

        memories = self.memories.list_active_memories(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type=memory_type,
            limit=limit,
        )
        return {"memories": [serialize_memory(memory) for memory in memories]}

    def list_workflows(self) -> JsonDict:
        """列出第一阶段可调用的金融团队 Workflow。"""

        workflows = [
            {
                "workflow_type": "portfolio_monitoring",
                "status": "langgraph_ready",
                "description": "持仓监控，已具备 LangGraph 包装入口，规则版保留为 fallback。",
            },
            {
                "workflow_type": "watchlist_management",
                "status": "langgraph_ready",
                "description": "私人观察池管理，已具备 LangGraph 包装入口，规则版保留为 fallback。",
            },
            {
                "workflow_type": "recommendation_decision",
                "status": "langgraph_ready",
                "description": "推荐入池、买入、卖出和换股决策，已具备 LangGraph 包装入口。",
            },
            {
                "workflow_type": "asset_deep_analysis",
                "status": "langgraph_skeleton",
                "description": "单标的深度分析，已具备 LangGraph 报告骨架。",
            },
            {
                "workflow_type": "swap_decision",
                "status": "langgraph_skeleton",
                "description": "弱持仓与强候选之间的换股或换币比较，已具备 LangGraph 报告骨架。",
            },
            {
                "workflow_type": "daily_review",
                "status": "langgraph_skeleton",
                "description": "每日持仓、观察池、推荐和风险复盘，已具备 LangGraph 报告骨架。",
            },
        ]
        return {"workflows": workflows}


def json_value(value: Any) -> Any:
    """把 ORM 字段转换为 JSON 友好的值。"""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    return value


def serialize_portfolio(portfolio: PortfolioORM) -> JsonDict:
    """序列化组合。"""

    return {
        "portfolio_id": portfolio.portfolio_id,
        "owner_id": portfolio.owner_id,
        "name": portfolio.name,
        "portfolio_type": portfolio.portfolio_type,
        "base_currency": portfolio.base_currency,
        "risk_profile": portfolio.risk_profile,
        "total_equity": json_value(portfolio.total_equity),
        "cash": json_value(portfolio.cash),
        "market_value": json_value(portfolio.market_value),
        "status": portfolio.status,
        "as_of": json_value(portfolio.as_of),
        "payload": json_value(portfolio.payload or {}),
    }


def serialize_position(position: PositionORM) -> JsonDict:
    """序列化持仓。"""

    return {
        "position_id": position.position_id,
        "portfolio_id": position.portfolio_id,
        "asset_id": position.asset_id,
        "symbol": position.symbol,
        "market": position.market,
        "side": position.side,
        "quantity": json_value(position.quantity),
        "avg_cost": json_value(position.avg_cost),
        "last_price": json_value(position.last_price),
        "market_value": json_value(position.market_value),
        "unrealized_pnl": json_value(position.unrealized_pnl),
        "unrealized_pnl_pct": json_value(position.unrealized_pnl_pct),
        "portfolio_weight": json_value(position.portfolio_weight),
        "status": position.status,
        "as_of": json_value(position.as_of),
        "payload": json_value(position.payload or {}),
    }


def serialize_watchlist_item(item: WatchlistItemORM) -> JsonDict:
    """序列化观察池条目。"""

    return {
        "watchlist_item_id": item.watchlist_item_id,
        "watchlist_id": item.watchlist_id,
        "asset_id": item.asset_id,
        "symbol": item.symbol,
        "market": item.market,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "reason": item.reason,
        "status": item.status,
        "risk_level": item.risk_level,
        "next_review_at": json_value(item.next_review_at),
        "watch_conditions": json_value(item.watch_conditions or {}),
        "trigger_conditions": json_value(item.trigger_conditions or {}),
        "invalid_conditions": json_value(item.invalid_conditions or {}),
        "payload": json_value(item.payload or {}),
    }


def serialize_recommendation_run(run: RecommendationRunORM) -> JsonDict:
    """序列化推荐运行。"""

    return {
        "run_id": run.run_id,
        "universe_id": run.universe_id,
        "screening_id": run.screening_id,
        "strategy": run.strategy,
        "market": run.market,
        "horizon": run.horizon,
        "limit": run.limit,
        "status": run.status,
        "started_at": json_value(run.started_at),
        "finished_at": json_value(run.finished_at),
        "summary": run.summary,
        "payload": json_value(run.payload or {}),
    }


def serialize_asset_recommendation(recommendation: Any) -> JsonDict:
    """序列化单标的推荐。"""

    return {
        "recommendation_id": recommendation.recommendation_id,
        "run_id": recommendation.run_id,
        "asset_id": recommendation.asset_id,
        "symbol": recommendation.symbol,
        "name": recommendation.name,
        "market": recommendation.market,
        "horizon": recommendation.horizon,
        "action": recommendation.action,
        "rank": recommendation.rank,
        "total_score": json_value(recommendation.total_score),
        "confidence": json_value(recommendation.confidence),
        "conviction": recommendation.conviction,
        "score_id": recommendation.score_id,
        "factor_frame_id": recommendation.factor_frame_id,
        "signal_ids": json_value(recommendation.signal_ids or []),
        "risk_ids": json_value(recommendation.risk_ids or []),
        "evidence_ids": json_value(recommendation.evidence_ids or []),
        "summary": recommendation.summary,
        "payload": json_value(recommendation.payload or {}),
    }


def serialize_indicator_frame(indicator: IndicatorFrameORM) -> JsonDict:
    """序列化 TA 技术指标快照。"""

    return {
        "indicator_frame_id": indicator.indicator_frame_id,
        "asset_id": indicator.asset_id,
        "symbol": indicator.symbol,
        "market": indicator.market,
        "timeframe": indicator.timeframe,
        "horizon": indicator.horizon,
        "library": indicator.library,
        "library_version": indicator.library_version,
        "input_start_at": json_value(indicator.input_start_at),
        "input_end_at": json_value(indicator.input_end_at),
        "bar_count": indicator.bar_count,
        "rsi_14": json_value(indicator.rsi_14),
        "macd": json_value(indicator.macd),
        "macd_signal": json_value(indicator.macd_signal),
        "macd_hist": json_value(indicator.macd_hist),
        "atr_14": json_value(indicator.atr_14),
        "bb_percent_b": json_value(indicator.bb_percent_b),
        "ma_20": json_value(indicator.ma_20),
        "ma_60": json_value(indicator.ma_60),
        "status": indicator.status,
        "as_of": json_value(indicator.as_of),
        "payload": json_value(indicator.payload or {}),
    }


def serialize_factor_frame(factor: FactorFrameORM) -> JsonDict:
    """序列化因子快照。"""

    return {
        "factor_frame_id": factor.factor_frame_id,
        "asset_id": factor.asset_id,
        "symbol": factor.symbol,
        "market": factor.market,
        "horizon": factor.horizon,
        "status": factor.status,
        "total_available_groups": factor.total_available_groups,
        "missing_groups": json_value(factor.missing_groups or []),
        "source_ids": json_value(factor.source_ids or []),
        "indicator_frame_id": factor.indicator_frame_id,
        "as_of": json_value(factor.as_of),
        "payload": json_value(factor.payload or {}),
    }


def serialize_asset_score(score: AssetScoreORM) -> JsonDict:
    """序列化多维评分。"""

    return {
        "score_id": score.score_id,
        "asset_id": score.asset_id,
        "symbol": score.symbol,
        "market": score.market,
        "universe_id": score.universe_id,
        "screening_id": score.screening_id,
        "factor_frame_id": score.factor_frame_id,
        "horizon": score.horizon,
        "total_score": json_value(score.total_score),
        "technical_score": json_value(score.technical_score),
        "fundamental_score": json_value(score.fundamental_score),
        "valuation_score": json_value(score.valuation_score),
        "flow_score": json_value(score.flow_score),
        "derivatives_score": json_value(score.derivatives_score),
        "event_score": json_value(score.event_score),
        "risk_penalty": json_value(score.risk_penalty),
        "missing_penalty": json_value(score.missing_penalty),
        "rank": score.rank,
        "rank_in_universe": score.rank_in_universe,
        "confidence": json_value(score.confidence),
        "rule_version": score.rule_version,
        "status": score.status,
        "as_of": json_value(score.as_of),
        "payload": json_value(score.payload or {}),
    }


def serialize_evidence(evidence: EvidenceORM) -> JsonDict:
    """序列化证据索引。"""

    return {
        "evidence_id": evidence.evidence_id,
        "evidence_type": evidence.evidence_type,
        "asset_id": evidence.asset_id,
        "source": evidence.source,
        "title": evidence.title,
        "summary": evidence.summary,
        "data_ref": evidence.data_ref,
        "url": evidence.url,
        "reliability": evidence.reliability,
        "as_of": json_value(evidence.as_of),
        "collected_at": json_value(evidence.collected_at),
        "payload": json_value(evidence.payload or {}),
    }


def serialize_signal(signal: SignalSnapshotORM) -> JsonDict:
    """序列化信号快照。"""

    payload = json_value(signal.payload or {})
    return {
        "signal_id": signal.signal_id,
        "asset_id": signal.asset_id,
        "horizon": signal.horizon,
        "direction": signal.direction,
        "score": json_value(signal.score),
        "confidence": json_value(signal.confidence),
        "as_of": json_value(signal.as_of),
        "signal_groups": payload.get("signal_groups", {}),
        "explanation": payload.get("explanation"),
        "payload": payload,
    }


def serialize_risk(risk: RiskFindingORM) -> JsonDict:
    """序列化风险发现。"""

    return {
        "risk_id": risk.risk_id,
        "asset_id": risk.asset_id,
        "risk_type": risk.risk_type,
        "severity": risk.severity,
        "title": risk.title,
        "description": risk.description,
        "evidence_ids": json_value(risk.evidence_ids or []),
        "detected_at": json_value(risk.as_of),
        "payload": json_value(risk.payload or {}),
    }


def serialize_data_quality(item: DataQualitySnapshotORM) -> JsonDict:
    """序列化数据质量快照。"""

    return {
        "quality_id": item.quality_id,
        "asset_id": item.asset_id,
        "symbol": item.symbol,
        "market": item.market,
        "data_domain": item.data_domain,
        "provider": item.provider,
        "status": item.status,
        "freshness_status": item.freshness_status,
        "latest_data_at": json_value(item.latest_data_at),
        "checked_at": json_value(item.checked_at),
        "missing_items": json_value(item.missing_items or []),
        "issue_count": item.issue_count,
        "payload": json_value(item.payload or {}),
    }


def serialize_memory(memory: AssistantMemoryORM) -> JsonDict:
    """序列化 Finance Memory。"""

    return {
        "memory_id": memory.memory_id,
        "owner_id": memory.owner_id,
        "memory_type": memory.memory_type,
        "scope": memory.scope,
        "asset_id": memory.asset_id,
        "source_decision_id": memory.source_decision_id,
        "source_review_task_id": memory.source_review_task_id,
        "content": memory.content,
        "confidence": json_value(memory.confidence),
        "status": memory.status,
        "updated_at": json_value(memory.updated_at),
        "payload": json_value(memory.payload or {}),
    }


def find_latest_score(
    session: Session,
    *,
    asset_id: str,
    horizon: str,
) -> AssetScoreORM | None:
    """查询单标的最新评分。"""

    from sqlalchemy import select

    statement = (
        select(AssetScoreORM)
        .where(AssetScoreORM.asset_id == asset_id, AssetScoreORM.horizon == horizon)
        .order_by(AssetScoreORM.as_of.desc())
        .limit(1)
    )
    return session.scalars(statement).one_or_none()


def list_recent_evidence(
    session: Session,
    *,
    asset_id: str,
    limit: int,
) -> list[EvidenceORM]:
    """查询单标的最近证据。"""

    from sqlalchemy import select

    statement = (
        select(EvidenceORM)
        .where(EvidenceORM.asset_id == asset_id)
        .order_by(EvidenceORM.as_of.desc().nullslast(), EvidenceORM.collected_at.desc())
        .limit(limit)
    )
    return list(session.scalars(statement))
