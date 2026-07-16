"""M0 仓储层。

仓储层只负责数据库读写和幂等更新，不承载采集、因子计算或推荐决策逻辑。
服务层后续可以组合这些仓储来跑通完整推荐链路。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, String, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_agent.storage.event_retention import (
    DEFAULT_EVENT_SIGNAL_LOOKBACK_DAYS,
    NEWS_ARTICLE_EVENT_TYPES,
    event_signal_cutoff,
)
from finance_agent.storage.event_validation import STOCK_NEWS_SOURCE, active_event_predicate
from finance_agent.storage.orm import (
    AgentWorkflowEventORM,
    AgentWorkflowRunORM,
    AssetORM,
    AssetProfileORM,
    AssetProviderMappingORM,
    AssetRecommendationORM,
    BacktestResultORM,
    AssetScoreORM,
    AssetStatusSnapshotORM,
    AssetThesisORM,
    AssetUniverseMemberORM,
    AssetUniverseORM,
    AssistantChatMessageORM,
    AssistantChatSessionORM,
    AssistantMemoryORM,
    AssistantTriggerEventORM,
    CapitalFlowSnapshotORM,
    CryptoDerivativeSnapshotORM,
    DataQualitySnapshotORM,
    DataSyncWatermarkORM,
    DecisionLogORM,
    EventRecordORM,
    EvidenceORM,
    ExecutionRecordORM,
    FactorFrameORM,
    FinancialMemoryEdgeORM,
    FundNavSnapshotORM,
    FundamentalSnapshotORM,
    IndicatorFrameORM,
    MarketBarORM,
    MarketCalendarORM,
    MemoryEmbeddingORM,
    ModelInstanceORM,
    ModelProviderORM,
    ModelRoutingRuleORM,
    MonitoringAlertORM,
    OrderDraftORM,
    PortfolioORM,
    PortfolioSnapshotORM,
    PositionORM,
    PositionSnapshotORM,
    RawRecordORM,
    RealtimeQuoteSnapshotORM,
    RecommendationRunORM,
    RecommendationRunUniverseORM,
    RetrievalProfileORM,
    ReviewTaskORM,
    RiskFindingORM,
    ScoringStrategyORM,
    ScreeningResultItemORM,
    ScreeningResultORM,
    SignalSnapshotORM,
    StrategyObservationOutcomeORM,
    StrategyObservationPositionORM,
    StrategyObservationRunORM,
    StrategyTrialStateORM,
    UserInvestmentProfileORM,
    WatchlistItemEventORM,
    WatchlistItemORM,
    WatchlistORM,
)

JsonDict = dict[str, Any]
FINAL_MARKET_BAR_STATUSES = ("available", "revised")
ACTION_LOOP_DISCLAIMER = (
    "非投资建议，仅用于用户自行决策前的操作草案；系统不会连接券商或交易所执行真实下单。"
)


def _ensure_not_mixed_market(market: str, *, context: str) -> None:
    """推荐链路中的候选池和推荐结果必须属于单一市场。"""

    if market == "mixed":
        raise ValueError(f"{context} 不能使用 mixed，A 股和数字货币必须走两条独立链路。")


def _ensure_same_market(
    *,
    expected: str,
    actual: str,
    context: str,
    subject: str,
) -> None:
    """校验同一次候选池或推荐运行中的市场一致性。"""

    if expected != actual:
        raise ValueError(
            f"{context} 市场为 {expected}，但 {subject} 属于 {actual}，不能跨市场混合。"
        )


def _json_safe(value: Any) -> Any:
    """把第三方响应转换成 PostgreSQL JSONB 可接受的结构。"""

    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return str(value)
    return str(value)


def _stable_json_hash(value: Any) -> str:
    """按稳定 JSON 表达计算 sha1，便于请求和内容追溯。"""

    safe_value = _json_safe(value)
    encoded = json.dumps(
        safe_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def _dedupe_rows(rows: Sequence[JsonDict], key_fields: Sequence[str]) -> list[JsonDict]:
    """按唯一键对批量入库行去重，避免同一条 INSERT 命中同一冲突行两次。"""

    deduped: dict[tuple[Any, ...], JsonDict] = {}
    for row in rows:
        deduped[tuple(row.get(field) for field in key_fields)] = row
    return list(deduped.values())


def _execute_chunked_upserts(
    session: Session,
    rows: Sequence[JsonDict],
    *,
    chunk_size: int,
    build_statement: Any,
) -> int:
    """按固定大小分块执行批量 upsert，并只在整批完成后 flush 一次。"""

    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if not rows:
        return 0
    total = 0
    for index in range(0, len(rows), chunk_size):
        chunk = rows[index : index + chunk_size]
        session.execute(build_statement(chunk))
        total += len(chunk)
    session.flush()
    return total


def _article_payload_update_rows(updates: Sequence[JsonDict]) -> list[JsonDict]:
    """标准化新闻正文回填行，并按事件 ID 去重。"""

    return _dedupe_rows(
        [
            {
                "event_id": item["event_id"],
                "article_payload": _json_safe(item.get("article_payload") or {}),
            }
            for item in updates
        ],
        ("event_id",),
    )


def _article_payload_update_statement(
    *,
    table_name: str,
    match_column: str,
    rows: Sequence[JsonDict],
) -> Any:
    """构造正文 payload 批量 JSONB 回填语句。"""

    params: dict[str, Any] = {}
    values_sql: list[str] = []
    for index, row in enumerate(rows):
        event_param = f"event_id_{index}"
        payload_param = f"article_payload_{index}"
        params[event_param] = row["event_id"]
        params[payload_param] = json.dumps(row["article_payload"], ensure_ascii=False)
        values_sql.append(f"(:{event_param}, CAST(:{payload_param} AS jsonb))")
    values_clause = ", ".join(values_sql)
    return text(
        f"""
        UPDATE {table_name} AS target
        SET payload = jsonb_set(
            COALESCE(target.payload, '{{}}'::jsonb),
            '{{article}}',
            source.article_payload,
            true
        )
        FROM (VALUES {values_clause}) AS source(event_id, article_payload)
        WHERE target.{match_column} = source.event_id
        """
    ).bindparams(**params)


def _entity_validation_update_rows(updates: Sequence[JsonDict]) -> list[JsonDict]:
    """标准化历史实体校验回填行，并按事件 ID 去重。"""

    return _dedupe_rows(
        [
            {
                "event_id": item["event_id"],
                "entity_validation": _json_safe(item.get("entity_validation") or {}),
            }
            for item in updates
        ],
        ("event_id",),
    )


def _entity_validation_update_statement(
    *,
    table_name: str,
    match_column: str,
    rows: Sequence[JsonDict],
) -> Any:
    """构造实体校验 JSONB 子对象批量合并语句。"""

    params: dict[str, Any] = {}
    values_sql: list[str] = []
    for index, row in enumerate(rows):
        event_param = f"event_id_{index}"
        validation_param = f"entity_validation_{index}"
        params[event_param] = row["event_id"]
        params[validation_param] = json.dumps(
            row["entity_validation"],
            ensure_ascii=False,
        )
        values_sql.append(f"(:{event_param}, CAST(:{validation_param} AS jsonb))")
    values_clause = ", ".join(values_sql)
    return text(
        f"""
        UPDATE {table_name} AS target
        SET payload = jsonb_set(
            COALESCE(target.payload, '{{}}'::jsonb),
            '{{entity_validation}}',
            source.entity_validation,
            true
        )
        FROM (VALUES {values_clause}) AS source(event_id, entity_validation)
        WHERE target.{match_column} = source.event_id
        """
    ).bindparams(**params)


def _execute_entity_validation_updates(
    session: Session,
    *,
    table_name: str,
    match_column: str,
    updates: Sequence[JsonDict],
    chunk_size: int,
) -> int:
    """分块合并实体校验 payload，并返回真实受影响行数。"""

    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    rows = _entity_validation_update_rows(updates)
    if not rows:
        return 0
    updated = 0
    for index in range(0, len(rows), chunk_size):
        chunk = rows[index : index + chunk_size]
        result = session.execute(
            _entity_validation_update_statement(
                table_name=table_name,
                match_column=match_column,
                rows=chunk,
            )
        )
        updated += int(result.rowcount or 0)
    session.flush()
    return updated


def _expired_article_delete_statement(
    *,
    table_name: str,
    type_column: str,
    time_column: str,
    event_types: Sequence[str],
    cutoff: datetime,
) -> Any:
    """构造过期新闻/公告整行删除语句。"""

    params: dict[str, Any] = {"cutoff": cutoff}
    type_placeholders: list[str] = []
    for index, event_type in enumerate(event_types):
        param_name = f"event_type_{index}"
        params[param_name] = event_type
        type_placeholders.append(f":{param_name}")
    return text(
        f"""
        DELETE FROM {table_name}
        WHERE {type_column} IN ({", ".join(type_placeholders)})
          AND (
            ({time_column} IS NOT NULL AND {time_column} < :cutoff)
            OR ({time_column} IS NULL AND collected_at < :cutoff)
          )
        """
    ).bindparams(**params)


class AssetRepository:
    """资产主数据和详情附表仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_asset(
        self,
        *,
        asset_id: str,
        symbol: str,
        name: str,
        market: str,
        asset_type: str,
        exchange: str | None = None,
        currency: str | None = None,
        sector: str | None = None,
        base_asset: str | None = None,
        quote_asset: str | None = None,
        tradable: bool = True,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> AssetORM:
        """按 `asset_id` 幂等写入资产。"""

        values = {
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "market": market,
            "asset_type": asset_type,
            "exchange": exchange,
            "currency": currency,
            "sector": sector,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "tradable": tradable,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssetORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key not in {"asset_id"}}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetORM.asset_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_asset(asset_id)

    def upsert_asset_master(
        self,
        *,
        asset_id: str,
        symbol: str,
        name: str,
        market: str,
        asset_type: str,
        exchange: str | None = None,
        currency: str | None = None,
        sector: str | None = None,
        base_asset: str | None = None,
        quote_asset: str | None = None,
        tradable: bool = True,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> AssetORM:
        """低频主数据刷新入口：只在稳定身份字段变化时修复 `assets` 主表。

        高频行情、资金流、财务和新闻采集仍应使用 `ensure_asset`，避免并发任务
        反复更新同一资产主表行。全市场资产池刷新属于低频权威来源，可以在
        占位资产先写入后回填名称、交易所、币种等稳定字段。
        """

        values = {
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "market": market,
            "asset_type": asset_type,
            "exchange": exchange,
            "currency": currency,
            "sector": sector,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "tradable": tradable,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssetORM).values(**values)
        stable_fields = (
            "symbol",
            "name",
            "market",
            "asset_type",
            "exchange",
            "currency",
            "sector",
            "base_asset",
            "quote_asset",
            "tradable",
            "status",
        )
        update_values = {key: statement.excluded[key] for key in stable_fields}
        update_values["payload"] = statement.excluded.payload
        update_values["updated_at"] = statement.excluded.updated_at
        changed_conditions = [
            getattr(AssetORM, key).is_distinct_from(statement.excluded[key])
            for key in stable_fields
        ]
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetORM.asset_id],
                set_=update_values,
                where=or_(*changed_conditions),
            )
        )
        self.session.flush()
        return self.get_asset(asset_id)

    def ensure_asset(
        self,
        *,
        asset_id: str,
        symbol: str,
        name: str,
        market: str,
        asset_type: str,
        exchange: str | None = None,
        currency: str | None = None,
        sector: str | None = None,
        base_asset: str | None = None,
        quote_asset: str | None = None,
        tradable: bool = True,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> AssetORM:
        """只在资产不存在时写入身份主表，存在时不更新主表行。

        基础采集任务会并发刷新行情、资金流、财务和事件。它们只需要确保
        `assets` 有稳定身份记录，动态字段应进入附表，避免并发 `DO UPDATE`
        反复争抢同一资产行。
        """

        values = {
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "market": market,
            "asset_type": asset_type,
            "exchange": exchange,
            "currency": currency,
            "sector": sector,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "tradable": tradable,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssetORM).values(**values)
        self.session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[AssetORM.asset_id],
            )
        )
        self.session.flush()
        return self.get_asset(asset_id)

    def upsert_asset_masters(
        self,
        assets: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量刷新低频权威资产主数据。"""

        now = datetime.now().astimezone()
        rows = _dedupe_rows(
            [
                {
                    "asset_id": item["asset_id"],
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "market": item["market"],
                    "asset_type": item["asset_type"],
                    "exchange": item.get("exchange"),
                    "currency": item.get("currency"),
                    "sector": item.get("sector"),
                    "base_asset": item.get("base_asset"),
                    "quote_asset": item.get("quote_asset"),
                    "tradable": item.get("tradable", True),
                    "status": item.get("status", "available"),
                    "payload": _json_safe(item.get("payload") or {}),
                    "updated_at": now,
                }
                for item in assets
            ],
            ("asset_id",),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(AssetORM).values(list(chunk))
            stable_fields = (
                "symbol",
                "name",
                "market",
                "asset_type",
                "exchange",
                "currency",
                "sector",
                "base_asset",
                "quote_asset",
                "tradable",
                "status",
            )
            update_values = {key: statement.excluded[key] for key in stable_fields}
            update_values["payload"] = statement.excluded.payload
            update_values["updated_at"] = statement.excluded.updated_at
            changed_conditions = [
                getattr(AssetORM, key).is_distinct_from(statement.excluded[key])
                for key in stable_fields
            ]
            return statement.on_conflict_do_update(
                index_elements=[AssetORM.asset_id],
                set_=update_values,
                where=or_(*changed_conditions),
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def delete_fund_open_placeholders_without_nav(self, symbols: Sequence[str]) -> int:
        """删除没有净值事实的开放式基金占位，避免与 ETF/LOF 主数据冲突。"""

        normalized_symbols = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
        if not normalized_symbols:
            return 0
        nav_exists = (
            select(FundNavSnapshotORM.asset_id)
            .where(FundNavSnapshotORM.asset_id == AssetORM.asset_id)
            .exists()
        )
        statement = delete(AssetORM).where(
            AssetORM.market == "fund",
            AssetORM.asset_type == "open_fund",
            AssetORM.symbol.in_(normalized_symbols),
            ~nav_exists,
        )
        result = self.session.execute(statement)
        self.session.flush()
        return int(result.rowcount or 0)

    def ensure_assets(
        self,
        assets: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量确保资产身份存在；已存在的主表行不做更新。"""

        now = datetime.now().astimezone()
        rows = _dedupe_rows(
            [
                {
                    "asset_id": item["asset_id"],
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "market": item["market"],
                    "asset_type": item["asset_type"],
                    "exchange": item.get("exchange"),
                    "currency": item.get("currency"),
                    "sector": item.get("sector"),
                    "base_asset": item.get("base_asset"),
                    "quote_asset": item.get("quote_asset"),
                    "tradable": item.get("tradable", True),
                    "status": item.get("status", "available"),
                    "payload": _json_safe(item.get("payload") or {}),
                    "updated_at": now,
                }
                for item in assets
            ],
            ("asset_id",),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(AssetORM).values(list(chunk))
            return statement.on_conflict_do_nothing(index_elements=[AssetORM.asset_id])

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def upsert_asset_profiles(
        self,
        profiles: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入资产画像附表。"""

        now = datetime.now().astimezone()
        rows = _dedupe_rows(
            [
                {
                    "asset_id": item["asset_id"],
                    "source": item["source"],
                    "name": item["name"],
                    "market": item["market"],
                    "symbol": item["symbol"],
                    "exchange": item.get("exchange"),
                    "sector": item.get("sector"),
                    "industry": item.get("industry"),
                    "concept": item.get("concept"),
                    "as_of": item["as_of"],
                    "payload": _json_safe(item.get("payload") or {}),
                    "updated_at": now,
                }
                for item in profiles
            ],
            ("asset_id", "source"),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(AssetProfileORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key]
                for key in rows[0]
                if key not in {"asset_id", "source"}
            }
            return statement.on_conflict_do_update(
                index_elements=[AssetProfileORM.asset_id, AssetProfileORM.source],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def upsert_asset_provider_mappings(
        self,
        mappings: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入 Provider 代码映射。"""

        now = datetime.now().astimezone()
        rows = _dedupe_rows(
            [
                {
                    "mapping_id": item.get("mapping_id")
                    or f"asset_mapping:{item['provider']}:{item['market']}:{item['provider_symbol']}".replace(
                        "/", "_"
                    ),
                    "asset_id": item["asset_id"],
                    "market": item["market"],
                    "symbol": item["symbol"],
                    "provider": item["provider"],
                    "provider_symbol": item["provider_symbol"],
                    "provider_exchange": item.get("provider_exchange"),
                    "source": item["source"],
                    "status": item.get("status", "available"),
                    "payload": _json_safe(item.get("payload") or {}),
                    "updated_at": now,
                }
                for item in mappings
            ],
            ("provider", "provider_symbol", "market"),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(AssetProviderMappingORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key] for key in rows[0] if key != "mapping_id"
            }
            return statement.on_conflict_do_update(
                constraint="uq_asset_provider_mappings_provider_symbol_market",
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def upsert_asset_status_snapshots(
        self,
        snapshots: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入资产交易状态快照。"""

        rows = _dedupe_rows(
            [
                {
                    "asset_id": item["asset_id"],
                    "as_of": item["as_of"],
                    "source": item["source"],
                    "symbol": item["symbol"],
                    "market": item["market"],
                    "tradable": item["tradable"],
                    "trading_status": item["trading_status"],
                    "reason": item.get("reason"),
                    "payload": _json_safe(item.get("payload") or {}),
                }
                for item in snapshots
            ],
            ("asset_id", "as_of", "source"),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(AssetStatusSnapshotORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key]
                for key in rows[0]
                if key not in {"asset_id", "as_of", "source"}
            }
            return statement.on_conflict_do_update(
                index_elements=[
                    AssetStatusSnapshotORM.asset_id,
                    AssetStatusSnapshotORM.as_of,
                    AssetStatusSnapshotORM.source,
                ],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def upsert_realtime_quote_snapshots(
        self,
        snapshots: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入实时行情快照。"""

        rows = _dedupe_rows(
            [
                {
                    "asset_id": item["asset_id"],
                    "as_of": item["as_of"],
                    "source": item["source"],
                    "symbol": item["symbol"],
                    "market": item["market"],
                    "last_price": item.get("last_price"),
                    "prev_close": item.get("prev_close"),
                    "open": item.get("open", item.get("open_price")),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "volume": item.get("volume"),
                    "amount": item.get("amount"),
                    "turnover_rate": item.get("turnover_rate"),
                    "change_amount": item.get("change_amount"),
                    "change_percent": item.get("change_percent"),
                    "bid_price": item.get("bid_price"),
                    "ask_price": item.get("ask_price"),
                    "status": item.get("status", "available"),
                    "payload": _json_safe(item.get("payload") or {}),
                }
                for item in snapshots
            ],
            ("asset_id", "as_of", "source"),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(RealtimeQuoteSnapshotORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key]
                for key in rows[0]
                if key not in {"asset_id", "as_of", "source"}
            }
            return statement.on_conflict_do_update(
                index_elements=[
                    RealtimeQuoteSnapshotORM.asset_id,
                    RealtimeQuoteSnapshotORM.as_of,
                    RealtimeQuoteSnapshotORM.source,
                ],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def upsert_asset_profile(
        self,
        *,
        asset_id: str,
        name: str,
        market: str,
        symbol: str,
        source: str,
        as_of: datetime,
        exchange: str | None = None,
        sector: str | None = None,
        industry: str | None = None,
        concept: str | None = None,
        payload: JsonDict | None = None,
    ) -> AssetProfileORM:
        """按 `asset_id + source` 幂等写入资产慢变资料。"""

        values = {
            "asset_id": asset_id,
            "source": source,
            "name": name,
            "market": market,
            "symbol": symbol,
            "exchange": exchange,
            "sector": sector,
            "industry": industry,
            "concept": concept,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssetProfileORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key not in {"asset_id", "source"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetProfileORM.asset_id, AssetProfileORM.source],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            AssetProfileORM,
            {
                "asset_id": asset_id,
                "source": source,
            },
        )

    def upsert_asset_provider_mapping(
        self,
        *,
        asset_id: str,
        market: str,
        symbol: str,
        provider: str,
        provider_symbol: str,
        source: str,
        provider_exchange: str | None = None,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> AssetProviderMappingORM:
        """按 Provider 代码唯一键幂等写入资产映射。"""

        mapping_id = f"asset_mapping:{provider}:{market}:{provider_symbol}".replace("/", "_")
        values = {
            "mapping_id": mapping_id,
            "asset_id": asset_id,
            "market": market,
            "symbol": symbol,
            "provider": provider,
            "provider_symbol": provider_symbol,
            "provider_exchange": provider_exchange,
            "source": source,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssetProviderMappingORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "mapping_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_asset_provider_mappings_provider_symbol_market",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssetProviderMappingORM, mapping_id)

    def upsert_asset_status_snapshot(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        as_of: datetime,
        source: str,
        tradable: bool,
        trading_status: str,
        reason: str | None = None,
        payload: JsonDict | None = None,
    ) -> AssetStatusSnapshotORM:
        """按 `asset_id + as_of + source` 幂等写入交易状态快照。"""

        values = {
            "asset_id": asset_id,
            "as_of": as_of,
            "source": source,
            "symbol": symbol,
            "market": market,
            "tradable": tradable,
            "trading_status": trading_status,
            "reason": reason,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssetStatusSnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"asset_id", "as_of", "source"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    AssetStatusSnapshotORM.asset_id,
                    AssetStatusSnapshotORM.as_of,
                    AssetStatusSnapshotORM.source,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            AssetStatusSnapshotORM,
            {
                "asset_id": asset_id,
                "as_of": as_of,
                "source": source,
            },
        )

    def upsert_realtime_quote_snapshot(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        as_of: datetime,
        source: str,
        last_price: Decimal | None = None,
        prev_close: Decimal | None = None,
        open_price: Decimal | None = None,
        high: Decimal | None = None,
        low: Decimal | None = None,
        volume: Decimal | None = None,
        amount: Decimal | None = None,
        turnover_rate: Decimal | None = None,
        change_amount: Decimal | None = None,
        change_percent: Decimal | None = None,
        bid_price: Decimal | None = None,
        ask_price: Decimal | None = None,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> RealtimeQuoteSnapshotORM:
        """按 `asset_id + as_of + source` 幂等写入实时行情快照。"""

        values = {
            "asset_id": asset_id,
            "as_of": as_of,
            "source": source,
            "symbol": symbol,
            "market": market,
            "last_price": last_price,
            "prev_close": prev_close,
            "open": open_price,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "turnover_rate": turnover_rate,
            "change_amount": change_amount,
            "change_percent": change_percent,
            "bid_price": bid_price,
            "ask_price": ask_price,
            "status": status,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(RealtimeQuoteSnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"asset_id", "as_of", "source"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    RealtimeQuoteSnapshotORM.asset_id,
                    RealtimeQuoteSnapshotORM.as_of,
                    RealtimeQuoteSnapshotORM.source,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            RealtimeQuoteSnapshotORM,
            {
                "asset_id": asset_id,
                "as_of": as_of,
                "source": source,
            },
        )

    def get_asset(self, asset_id: str) -> AssetORM:
        """根据资产 ID 查询资产，不存在则抛错。"""

        return self.session.get_one(AssetORM, asset_id)

    def get_asset_or_none(self, asset_id: str) -> AssetORM | None:
        """根据资产 ID 查询资产，不存在时返回空。"""

        return self.session.get(AssetORM, asset_id)

    def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[AssetORM]:
        """按市场查询资产列表。"""

        statement: Select[tuple[AssetORM]] = select(AssetORM).where(AssetORM.market == market)
        if only_tradable:
            statement = statement.where(AssetORM.tradable.is_(True))
        return list(self.session.scalars(statement.order_by(AssetORM.symbol)))


class RawRecordRepository:
    """Provider 原始响应归档仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_raw_record(
        self,
        *,
        provider: str,
        endpoint: str,
        request_params: JsonDict,
        response_payload: JsonDict,
        status: str,
        collected_at: datetime,
        asset_id: str | None = None,
        symbol: str | None = None,
        market: str | None = None,
        provider_version: str | None = None,
        latency_ms: int | None = None,
        retry_count: int = 0,
        error_message: str | None = None,
        as_of: datetime | None = None,
        raw_record_id: str | None = None,
    ) -> RawRecordORM:
        """追加写入一条原始响应记录。

        `raw_records` 是审计表，默认不按请求覆盖历史。调用方如需要幂等，
        可以显式传入 `raw_record_id`。
        """

        safe_request = _json_safe(request_params)
        safe_response = _json_safe(response_payload)
        request_hash = _stable_json_hash(safe_request)
        content_hash = _stable_json_hash(safe_response)
        record_id = raw_record_id or (
            f"raw:{provider}:{endpoint}:{collected_at.isoformat()}:{request_hash[:12]}:"
            f"{content_hash[:12]}"
        )
        values = {
            "raw_record_id": record_id,
            "provider": provider,
            "endpoint": endpoint,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "request_params": safe_request,
            "request_hash": request_hash,
            "response_payload": safe_response,
            "content_hash": content_hash,
            "provider_version": provider_version,
            "latency_ms": latency_ms,
            "retry_count": retry_count,
            "status": status,
            "error_message": error_message,
            "as_of": as_of,
            "collected_at": collected_at,
        }
        statement = insert(RawRecordORM).values(**values)
        update_values = {
            "asset_id": statement.excluded.asset_id,
            "symbol": statement.excluded.symbol,
            "market": statement.excluded.market,
            "request_params": statement.excluded.request_params,
            "response_payload": statement.excluded.response_payload,
            "provider_version": statement.excluded.provider_version,
            "latency_ms": statement.excluded.latency_ms,
            "retry_count": statement.excluded.retry_count,
            "error_message": statement.excluded.error_message,
            "as_of": statement.excluded.as_of,
            "collected_at": func.greatest(
                RawRecordORM.collected_at,
                statement.excluded.collected_at,
            ),
        }
        result = self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    RawRecordORM.provider,
                    RawRecordORM.endpoint,
                    RawRecordORM.request_hash,
                    RawRecordORM.content_hash,
                    RawRecordORM.status,
                ],
                set_=update_values,
            ).returning(RawRecordORM.raw_record_id)
        )
        self.session.flush()
        return self.session.get_one(RawRecordORM, result.scalar_one())

    def count_by_provider(self, provider: str) -> int:
        """统计某个 Provider 已归档的原始记录数量。"""

        statement = (
            select(func.count()).select_from(RawRecordORM).where(RawRecordORM.provider == provider)
        )
        return int(self.session.scalar(statement) or 0)


class DataSyncWatermarkRepository:
    """数据采集水位和失败重试状态仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record_success(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        data_domain: str,
        provider: str,
        timeframe: str | None = None,
        watermark_at: datetime | None = None,
        occurred_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> None:
        """记录采集成功水位，并清空失败重试状态。"""

        now = occurred_at or datetime.now().astimezone()
        values = {
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "data_domain": data_domain,
            "timeframe": timeframe or "",
            "provider": provider,
            "status": "available",
            "watermark_at": watermark_at,
            "last_success_at": now,
            "last_failed_at": None,
            "next_retry_at": None,
            "fail_count": 0,
            "last_error_message": None,
            "payload": _json_safe(payload or {}),
            "updated_at": now,
        }
        statement = insert(DataSyncWatermarkORM).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="pk_data_sync_watermarks",
                set_={
                    "symbol": statement.excluded.symbol,
                    "market": statement.excluded.market,
                    "status": statement.excluded.status,
                    "watermark_at": statement.excluded.watermark_at,
                    "last_success_at": statement.excluded.last_success_at,
                    "last_failed_at": None,
                    "next_retry_at": None,
                    "fail_count": 0,
                    "last_error_message": None,
                    "payload": statement.excluded.payload,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
        self.session.flush()

    def record_failure(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        data_domain: str,
        provider: str,
        timeframe: str | None = None,
        occurred_at: datetime | None = None,
        retry_after: timedelta | None = timedelta(minutes=15),
        error_message: str | None = None,
        payload: JsonDict | None = None,
    ) -> None:
        """记录采集失败，并设置下一次可重试时间。"""

        now = occurred_at or datetime.now().astimezone()
        next_retry_at = now + retry_after if retry_after is not None else None
        values = {
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "data_domain": data_domain,
            "timeframe": timeframe or "",
            "provider": provider,
            "status": "error",
            "watermark_at": None,
            "last_success_at": None,
            "last_failed_at": now,
            "next_retry_at": next_retry_at,
            "fail_count": 1,
            "last_error_message": error_message,
            "payload": _json_safe(payload or {}),
            "updated_at": now,
        }
        statement = insert(DataSyncWatermarkORM).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="pk_data_sync_watermarks",
                set_={
                    "symbol": statement.excluded.symbol,
                    "market": statement.excluded.market,
                    "status": statement.excluded.status,
                    "last_failed_at": statement.excluded.last_failed_at,
                    "next_retry_at": statement.excluded.next_retry_at,
                    "fail_count": DataSyncWatermarkORM.fail_count + 1,
                    "last_error_message": statement.excluded.last_error_message,
                    "payload": statement.excluded.payload,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
        self.session.flush()

    def record_unavailable(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        data_domain: str,
        provider: str,
        timeframe: str | None = None,
        occurred_at: datetime | None = None,
        error_message: str | None = None,
        payload: JsonDict | None = None,
    ) -> None:
        """记录源端确认无数据的终态水位，不安排自动重试。"""

        now = occurred_at or datetime.now().astimezone()
        values = {
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "data_domain": data_domain,
            "timeframe": timeframe or "",
            "provider": provider,
            "status": "unavailable",
            "watermark_at": None,
            "last_success_at": None,
            "last_failed_at": now,
            "next_retry_at": None,
            "fail_count": 1,
            "last_error_message": error_message,
            "payload": _json_safe(payload or {}),
            "updated_at": now,
        }
        statement = insert(DataSyncWatermarkORM).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="pk_data_sync_watermarks",
                set_={
                    "symbol": statement.excluded.symbol,
                    "market": statement.excluded.market,
                    "status": statement.excluded.status,
                    "last_failed_at": statement.excluded.last_failed_at,
                    "next_retry_at": None,
                    "fail_count": DataSyncWatermarkORM.fail_count + 1,
                    "last_error_message": statement.excluded.last_error_message,
                    "payload": statement.excluded.payload,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
        self.session.flush()

    def get_next_retry_at(
        self,
        *,
        asset_id: str,
        data_domain: str,
        provider: str,
        timeframe: str | None = None,
    ) -> datetime | None:
        """读取指定资产和数据域的下一次可重试时间。"""

        statement = select(DataSyncWatermarkORM.next_retry_at).where(
            DataSyncWatermarkORM.asset_id == asset_id,
            DataSyncWatermarkORM.data_domain == data_domain,
            DataSyncWatermarkORM.provider == provider,
            DataSyncWatermarkORM.timeframe == (timeframe or ""),
        )
        return self.session.execute(statement).scalar_one_or_none()


class FundNavRepository:
    """开放式基金净值仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_snapshot(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        nav_date: date,
        source: str,
        unit_nav: Decimal | None = None,
        accumulated_nav: Decimal | None = None,
        daily_return: Decimal | None = None,
        purchase_status: str | None = None,
        redeem_status: str | None = None,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> FundNavSnapshotORM:
        """按来源、资产和净值日期幂等写入净值快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "nav_date": nav_date,
            "unit_nav": unit_nav,
            "accumulated_nav": accumulated_nav,
            "daily_return": daily_return,
            "purchase_status": purchase_status,
            "redeem_status": redeem_status,
            "source": source,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(FundNavSnapshotORM).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_fund_nav_snapshots_source_asset_nav_date",
                set_={
                    key: statement.excluded[key]
                    for key in values
                    if key not in {"snapshot_id", "asset_id", "nav_date", "source"}
                },
            )
        )
        self.session.flush()
        return self.get_snapshot(snapshot_id)

    def upsert_snapshots(
        self,
        snapshots: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入开放式基金净值快照。"""

        now = datetime.now().astimezone()
        rows = _dedupe_rows(
            [
                {
                    "snapshot_id": item["snapshot_id"],
                    "asset_id": item["asset_id"],
                    "symbol": item["symbol"],
                    "market": item["market"],
                    "nav_date": item["nav_date"],
                    "unit_nav": item.get("unit_nav"),
                    "accumulated_nav": item.get("accumulated_nav"),
                    "daily_return": item.get("daily_return"),
                    "purchase_status": item.get("purchase_status"),
                    "redeem_status": item.get("redeem_status"),
                    "source": item["source"],
                    "status": item.get("status", "available"),
                    "payload": _json_safe(item.get("payload") or {}),
                    "updated_at": now,
                }
                for item in snapshots
            ],
            ("source", "asset_id", "nav_date"),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(FundNavSnapshotORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key]
                for key in rows[0]
                if key not in {"snapshot_id", "asset_id", "nav_date", "source"}
            }
            return statement.on_conflict_do_update(
                constraint="uq_fund_nav_snapshots_source_asset_nav_date",
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def get_snapshot(self, snapshot_id: str) -> FundNavSnapshotORM:
        """按快照 ID 读取净值快照。"""

        return self.session.get_one(FundNavSnapshotORM, snapshot_id)

    def list_recent_snapshots(self, *, asset_id: str, limit: int) -> list[FundNavSnapshotORM]:
        """读取单只基金最近净值记录。"""

        statement = (
            select(FundNavSnapshotORM)
            .where(FundNavSnapshotORM.asset_id == asset_id)
            .order_by(FundNavSnapshotORM.nav_date.desc())
            .limit(limit)
        )
        rows = list(self.session.scalars(statement))
        return list(reversed(rows))


class UniverseRepository:
    """候选池仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_universe(
        self,
        *,
        universe_id: str,
        name: str,
        source: str,
        market: str,
        as_of: datetime,
        strategy_context: str | None = None,
        owner_id: str | None = None,
        visibility: str = "system",
        base_universe_id: str | None = None,
        total_before_filter: int | None = None,
        total_after_filter: int | None = None,
        filters: JsonDict | None = None,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> AssetUniverseORM:
        """按 `universe_id` 幂等写入候选池定义。"""

        _ensure_not_mixed_market(market, context="候选池")
        values = {
            "universe_id": universe_id,
            "name": name,
            "source": source,
            "market": market,
            "strategy_context": strategy_context,
            "owner_id": owner_id,
            "visibility": visibility,
            "base_universe_id": base_universe_id,
            "total_before_filter": total_before_filter,
            "total_after_filter": total_after_filter,
            "filters": _json_safe(filters or {}),
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssetUniverseORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key not in {"universe_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetUniverseORM.universe_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_universe(universe_id)

    def upsert_member(
        self,
        *,
        member_id: str,
        universe_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        as_of: datetime,
        included: bool = True,
        removed_reason: str | None = None,
        rank_hint: int | None = None,
        payload: JsonDict | None = None,
    ) -> AssetUniverseMemberORM:
        """按 `universe_id + asset_id` 幂等写入候选池成员。"""

        _ensure_not_mixed_market(market, context="候选池成员")
        universe = self.get_universe(universe_id)
        _ensure_same_market(
            expected=universe.market,
            actual=market,
            context=f"候选池 {universe_id}",
            subject=f"成员 {asset_id}",
        )
        values = {
            "id": member_id,
            "universe_id": universe_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "included": included,
            "removed_reason": removed_reason,
            "rank_hint": rank_hint,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssetUniverseMemberORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"id", "universe_id", "asset_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_universe_members_universe_asset",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_member(universe_id=universe_id, asset_id=asset_id)

    def replace_members(
        self,
        *,
        universe_id: str,
        members: Sequence[dict[str, Any]],
    ) -> list[AssetUniverseMemberORM]:
        """批量写入候选池成员。

        第一版采用逐条 upsert，优先保证语义清晰。后续候选池规模变大时，
        可以改成批量 insert + on conflict。
        """

        if not members:
            return []
        universe = self.get_universe(universe_id)
        rows: list[JsonDict] = []
        for member in members:
            market = member["market"]
            _ensure_not_mixed_market(market, context="鍊欓€夋睜鎴愬憳")
            _ensure_same_market(
                expected=universe.market,
                actual=market,
                context=f"鍊欓€夋睜 {universe_id}",
                subject=f"鎴愬憳 {member['asset_id']}",
            )
            rows.append(
                {
                    "id": member.get("id") or member["member_id"],
                    "universe_id": universe_id,
                    "asset_id": member["asset_id"],
                    "symbol": member["symbol"],
                    "market": market,
                    "included": member.get("included", True),
                    "removed_reason": member.get("removed_reason"),
                    "rank_hint": member.get("rank_hint"),
                    "as_of": member["as_of"],
                    "payload": _json_safe(member.get("payload") or {}),
                }
            )
        rows = _dedupe_rows(rows, ("universe_id", "asset_id"))
        member_ids = [row["id"] for row in rows]

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(AssetUniverseMemberORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key]
                for key in rows[0]
                if key not in {"id", "universe_id", "asset_id"}
            }
            return statement.on_conflict_do_update(
                constraint="uq_universe_members_universe_asset",
                set_=update_values,
            )

        _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=500,
            build_statement=build_statement,
        )
        statement = select(AssetUniverseMemberORM).where(AssetUniverseMemberORM.id.in_(member_ids))
        return list(self.session.scalars(statement))

    def prune_missing_members(
        self,
        *,
        universe_id: str,
        current_asset_ids: Sequence[str],
        as_of: datetime,
        removed_reason: str = "not_in_latest_rebuild",
    ) -> int:
        """显式剔除本轮重建缺席的候选池成员。

        `replace_members` 保留 upsert 语义，调用方只有在“本轮结果代表完整快照”
        时才应调用本方法，把旧的 included 成员标记为 excluded。
        """

        active_asset_ids = sorted({str(asset_id).strip() for asset_id in current_asset_ids if asset_id})
        statement = (
            update(AssetUniverseMemberORM)
            .where(AssetUniverseMemberORM.universe_id == universe_id)
            .where(AssetUniverseMemberORM.included.is_(True))
            .values(
                included=False,
                removed_reason=removed_reason,
                as_of=as_of,
            )
        )
        if active_asset_ids:
            statement = statement.where(AssetUniverseMemberORM.asset_id.not_in(active_asset_ids))
        result = self.session.execute(statement)
        self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    def get_universe(self, universe_id: str) -> AssetUniverseORM:
        """根据候选池 ID 查询候选池。"""

        return self.session.get_one(AssetUniverseORM, universe_id)

    def list_universes(self, universe_ids: Sequence[str]) -> list[AssetUniverseORM]:
        """按 ID 列表查询候选池定义。"""

        if not universe_ids:
            return []
        statement = select(AssetUniverseORM).where(AssetUniverseORM.universe_id.in_(universe_ids))
        return list(self.session.scalars(statement.order_by(AssetUniverseORM.universe_id)))

    def get_member(self, *, universe_id: str, asset_id: str) -> AssetUniverseMemberORM:
        """根据候选池和资产 ID 查询成员。"""

        statement = select(AssetUniverseMemberORM).where(
            AssetUniverseMemberORM.universe_id == universe_id,
            AssetUniverseMemberORM.asset_id == asset_id,
        )
        return self.session.scalars(statement).one()

    def list_members(
        self, universe_id: str, *, included_only: bool = True
    ) -> list[AssetUniverseMemberORM]:
        """查询候选池成员。"""

        statement = select(AssetUniverseMemberORM).where(
            AssetUniverseMemberORM.universe_id == universe_id
        )
        if included_only:
            statement = statement.where(AssetUniverseMemberORM.included.is_(True))
        return list(self.session.scalars(statement.order_by(AssetUniverseMemberORM.symbol)))


class MarketCalendarRepository:
    """交易日历仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_calendar_entry(
        self,
        *,
        calendar_id: str,
        market: str,
        exchange: str,
        trade_date: date,
        is_trading_day: bool,
        session_type: str,
        timezone: str,
        source: str,
        open_at: datetime | None = None,
        close_at: datetime | None = None,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> MarketCalendarORM:
        """按市场、交易所、日期和 session 幂等写入交易日历。"""

        values = {
            "calendar_id": calendar_id,
            "market": market,
            "exchange": exchange,
            "trade_date": trade_date,
            "is_trading_day": is_trading_day,
            "open_at": open_at,
            "close_at": close_at,
            "session_type": session_type,
            "timezone": timezone,
            "status": status,
            "source": source,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(MarketCalendarORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"calendar_id", "market", "exchange", "trade_date", "session_type"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_market_calendars_session",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_calendar_entry(
            market=market,
            exchange=exchange,
            trade_date=trade_date,
            session_type=session_type,
        )

    def replace_calendar_entries(
        self,
        entries: Sequence[dict[str, Any]],
    ) -> list[MarketCalendarORM]:
        """批量幂等写入交易日历。"""

        rows = _dedupe_rows(
            [
                {
                    "calendar_id": entry["calendar_id"],
                    "market": entry["market"],
                    "exchange": entry["exchange"],
                    "trade_date": entry["trade_date"],
                    "is_trading_day": entry["is_trading_day"],
                    "open_at": entry.get("open_at"),
                    "close_at": entry.get("close_at"),
                    "session_type": entry["session_type"],
                    "timezone": entry["timezone"],
                    "status": entry.get("status", "available"),
                    "source": entry["source"],
                    "payload": _json_safe(entry.get("payload") or {}),
                }
                for entry in entries
            ],
            ("market", "exchange", "trade_date", "session_type"),
        )
        calendar_ids = [row["calendar_id"] for row in rows]

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(MarketCalendarORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key]
                for key in rows[0]
                if key not in {"calendar_id", "market", "exchange", "trade_date", "session_type"}
            }
            return statement.on_conflict_do_update(
                constraint="uq_market_calendars_session",
                set_=update_values,
            )

        _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=500,
            build_statement=build_statement,
        )
        if not calendar_ids:
            return []
        statement = select(MarketCalendarORM).where(MarketCalendarORM.calendar_id.in_(calendar_ids))
        return list(self.session.scalars(statement))

    def get_calendar_entry(
        self,
        *,
        market: str,
        exchange: str,
        trade_date: date,
        session_type: str = "regular",
    ) -> MarketCalendarORM:
        """查询单个交易日历条目。"""

        statement = select(MarketCalendarORM).where(
            MarketCalendarORM.market == market,
            MarketCalendarORM.exchange == exchange,
            MarketCalendarORM.trade_date == trade_date,
            MarketCalendarORM.session_type == session_type,
        )
        return self.session.scalars(statement).one()

    def list_calendar_entries(
        self,
        *,
        market: str,
        start_date: date,
        end_date: date,
        exchange: str | None = None,
    ) -> list[MarketCalendarORM]:
        """查询时间窗口内的交易日历。"""

        statement = select(MarketCalendarORM).where(
            MarketCalendarORM.market == market,
            MarketCalendarORM.trade_date >= start_date,
            MarketCalendarORM.trade_date <= end_date,
        )
        if exchange:
            statement = statement.where(MarketCalendarORM.exchange == exchange)
        return list(self.session.scalars(statement.order_by(MarketCalendarORM.trade_date)))


class MarketDataRepository:
    """标准行情仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_bar(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        timestamp: datetime,
        open_price: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: Decimal,
        source: str,
        adjustment: str = "",
        end_timestamp: datetime | None = None,
        amount: Decimal | None = None,
        is_closed: bool = True,
        raw_record_id: str | None = None,
        status: str = "available",
    ) -> MarketBarORM:
        """按 K 线唯一键幂等写入行情。"""

        self.upsert_bars(
            [
                {
                    "asset_id": asset_id,
                    "symbol": symbol,
                    "market": market,
                    "timeframe": timeframe,
                    "timestamp": timestamp,
                    "end_timestamp": end_timestamp,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": amount,
                    "source": source,
                    "adjustment": adjustment,
                    "is_closed": is_closed,
                    "raw_record_id": raw_record_id,
                    "status": status,
                }
            ]
        )
        return self.get_bar(
            asset_id=asset_id,
            timeframe=timeframe,
            timestamp=timestamp,
            source=source,
            adjustment=adjustment,
        )

    def upsert_bars(self, bars: Sequence[JsonDict], *, chunk_size: int = 500) -> int:
        """按批次幂等写入 K 线，默认每批 500 条，降低大量历史 K 线入库的数据库往返。"""

        if not bars:
            return 0

        conflict_keys = ("asset_id", "timeframe", "timestamp", "source", "adjustment")
        deduplicated_rows: dict[tuple[Any, ...], JsonDict] = {}
        ordered_keys: list[tuple[Any, ...]] = []
        for row in bars:
            row_values = dict(row)
            conflict_key = tuple(row_values[key] for key in conflict_keys)
            if conflict_key not in deduplicated_rows:
                ordered_keys.append(conflict_key)
            deduplicated_rows[conflict_key] = row_values

        rows = [deduplicated_rows[key] for key in ordered_keys]
        normalized_chunk_size = max(int(chunk_size), 1)
        row_count = 0
        for offset in range(0, len(rows), normalized_chunk_size):
            chunk = rows[offset : offset + normalized_chunk_size]
            statement = insert(MarketBarORM).values(chunk)
            update_values = {
                key: statement.excluded[key] for key in chunk[0] if key not in conflict_keys
            }
            self.session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        MarketBarORM.asset_id,
                        MarketBarORM.timeframe,
                        MarketBarORM.timestamp,
                        MarketBarORM.source,
                        MarketBarORM.adjustment,
                    ],
                    set_=update_values,
                )
            )
            row_count += len(chunk)
        self.session.flush()
        return row_count

    def get_bar(
        self,
        *,
        asset_id: str,
        timeframe: str,
        timestamp: datetime,
        source: str,
        adjustment: str = "",
    ) -> MarketBarORM:
        """根据复合键查询单根 K 线。"""

        return self.session.get_one(
            MarketBarORM,
            {
                "asset_id": asset_id,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "source": source,
                "adjustment": adjustment,
            },
        )

    def list_recent_bars(
        self,
        *,
        asset_id: str,
        timeframe: str,
        limit: int,
        source: str | None = None,
        closed_only: bool = True,
    ) -> list[MarketBarORM]:
        """查询单标的最近 N 根 K 线，返回时间升序结果。"""

        statement = select(MarketBarORM).where(
            MarketBarORM.asset_id == asset_id,
            MarketBarORM.timeframe == timeframe,
        )
        if source:
            statement = statement.where(MarketBarORM.source == source)
        if closed_only:
            statement = statement.where(
                MarketBarORM.is_closed.is_(True),
                MarketBarORM.status.in_(FINAL_MARKET_BAR_STATUSES),
            )

        rows = list(
            self.session.scalars(statement.order_by(MarketBarORM.timestamp.desc()).limit(limit))
        )
        return list(reversed(rows))

    def list_window_bars(
        self,
        *,
        asset_ids: Sequence[str],
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
        source: str | None = None,
    ) -> list[MarketBarORM]:
        """批量查询一组资产在时间窗口内的 K 线。"""

        statement = select(MarketBarORM).where(
            MarketBarORM.asset_id.in_(asset_ids),
            MarketBarORM.timeframe == timeframe,
            MarketBarORM.timestamp >= start_at,
            MarketBarORM.timestamp < end_at,
        )
        if source:
            statement = statement.where(MarketBarORM.source == source)
        statement = statement.where(MarketBarORM.status.in_(FINAL_MARKET_BAR_STATUSES))
        return list(
            self.session.scalars(statement.order_by(MarketBarORM.asset_id, MarketBarORM.timestamp))
        )


class IndicatorFrameRepository:
    """技术指标结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_indicator_frame(
        self,
        *,
        indicator_frame_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        horizon: str,
        library: str,
        input_start_at: datetime,
        input_end_at: datetime,
        bar_count: int,
        status: str,
        as_of: datetime,
        library_version: str | None = None,
        rsi_14: Decimal | None = None,
        macd: Decimal | None = None,
        macd_signal: Decimal | None = None,
        macd_hist: Decimal | None = None,
        atr_14: Decimal | None = None,
        bb_percent_b: Decimal | None = None,
        ma_20: Decimal | None = None,
        ma_60: Decimal | None = None,
        payload: JsonDict | None = None,
    ) -> IndicatorFrameORM:
        """按指标输入唯一键幂等写入技术指标结果。"""

        values = {
            "indicator_frame_id": indicator_frame_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "timeframe": timeframe,
            "horizon": horizon,
            "library": library,
            "library_version": library_version,
            "input_start_at": input_start_at,
            "input_end_at": input_end_at,
            "bar_count": bar_count,
            "rsi_14": rsi_14,
            "macd": macd,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "atr_14": atr_14,
            "bb_percent_b": bb_percent_b,
            "ma_20": ma_20,
            "ma_60": ma_60,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(IndicatorFrameORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key
            not in {
                "indicator_frame_id",
                "asset_id",
                "timeframe",
                "horizon",
                "library",
                "input_end_at",
            }
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_indicator_frames_input",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_latest_indicator_frame(
            asset_id=asset_id,
            timeframe=timeframe,
            horizon=horizon,
            library=library,
        )

    def get_latest_indicator_frame(
        self,
        *,
        asset_id: str,
        timeframe: str,
        horizon: str,
        library: str | None = None,
    ) -> IndicatorFrameORM | None:
        """查询单标的最新技术指标结果。"""

        statement = select(IndicatorFrameORM).where(
            IndicatorFrameORM.asset_id == asset_id,
            IndicatorFrameORM.timeframe == timeframe,
            IndicatorFrameORM.horizon == horizon,
        )
        if library:
            statement = statement.where(IndicatorFrameORM.library == library)
        return self.session.scalars(
            statement.order_by(IndicatorFrameORM.input_end_at.desc()).limit(1)
        ).one_or_none()


class FactorFrameRepository:
    """推荐因子结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_factor_frame(
        self,
        *,
        factor_frame_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        horizon: str,
        status: str,
        total_available_groups: int,
        missing_groups: list[str],
        source_ids: list[str],
        as_of: datetime,
        indicator_frame_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> FactorFrameORM:
        """按 `factor_frame_id` 幂等写入因子结果。"""

        values = {
            "factor_frame_id": factor_frame_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "horizon": horizon,
            "status": status,
            "total_available_groups": total_available_groups,
            "missing_groups": missing_groups,
            "source_ids": source_ids,
            "indicator_frame_id": indicator_frame_id,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(FactorFrameORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "factor_frame_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[FactorFrameORM.factor_frame_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(FactorFrameORM, factor_frame_id)

    def get_latest_factor_frame(
        self,
        *,
        asset_id: str,
        horizon: str,
    ) -> FactorFrameORM | None:
        """查询单标的最新因子结果。"""

        statement = select(FactorFrameORM).where(
            FactorFrameORM.asset_id == asset_id,
            FactorFrameORM.horizon == horizon,
        )
        return self.session.scalars(
            statement.order_by(FactorFrameORM.as_of.desc()).limit(1)
        ).one_or_none()


class ScreeningRepository:
    """候选池初筛结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_screening_result(
        self,
        *,
        screening_id: str,
        universe_id: str,
        strategy: str,
        market: str,
        passed_count: int,
        removed_count: int,
        rules: JsonDict,
        status: str,
        as_of: datetime,
        payload: JsonDict | None = None,
    ) -> ScreeningResultORM:
        """按 `screening_id` 幂等写入初筛汇总。"""

        values = {
            "screening_id": screening_id,
            "universe_id": universe_id,
            "strategy": strategy,
            "market": market,
            "passed_count": passed_count,
            "removed_count": removed_count,
            "rules": _json_safe(rules),
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(ScreeningResultORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "screening_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[ScreeningResultORM.screening_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(ScreeningResultORM, screening_id)

    def upsert_screening_item(
        self,
        *,
        screening_item_id: str,
        screening_id: str,
        universe_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        passed: bool,
        data_status: str,
        as_of: datetime,
        removed_reason: str | None = None,
        failed_rules: list[str] | None = None,
        passed_rules: list[str] | None = None,
        liquidity_status: str | None = None,
        payload: JsonDict | None = None,
    ) -> ScreeningResultItemORM:
        """按 `screening_id + asset_id` 幂等写入初筛明细。"""

        values = {
            "screening_item_id": screening_item_id,
            "screening_id": screening_id,
            "universe_id": universe_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "passed": passed,
            "removed_reason": removed_reason,
            "failed_rules": failed_rules or [],
            "passed_rules": passed_rules or [],
            "data_status": data_status,
            "liquidity_status": liquidity_status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(ScreeningResultItemORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"screening_item_id", "screening_id", "asset_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_screening_items_screening_asset",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_screening_item(screening_id=screening_id, asset_id=asset_id)

    def get_screening_result(self, screening_id: str) -> ScreeningResultORM:
        """查询初筛汇总。"""

        return self.session.get_one(ScreeningResultORM, screening_id)

    def get_latest_screening_result(
        self,
        *,
        market: str,
        strategy: str,
    ) -> ScreeningResultORM | None:
        """查询某市场某策略最近一次初筛汇总。"""

        statement = (
            select(ScreeningResultORM)
            .where(
                ScreeningResultORM.market == market,
                ScreeningResultORM.strategy == strategy,
            )
            .order_by(ScreeningResultORM.as_of.desc())
            .limit(1)
        )
        return self.session.scalars(statement).one_or_none()

    def get_screening_item(
        self,
        *,
        screening_id: str,
        asset_id: str,
    ) -> ScreeningResultItemORM:
        """查询单条初筛明细。"""

        statement = select(ScreeningResultItemORM).where(
            ScreeningResultItemORM.screening_id == screening_id,
            ScreeningResultItemORM.asset_id == asset_id,
        )
        return self.session.scalars(statement).one()

    def list_items(
        self,
        *,
        screening_id: str,
        passed_only: bool = False,
    ) -> list[ScreeningResultItemORM]:
        """查询初筛明细。"""

        statement = select(ScreeningResultItemORM).where(
            ScreeningResultItemORM.screening_id == screening_id
        )
        if passed_only:
            statement = statement.where(ScreeningResultItemORM.passed.is_(True))
        return list(self.session.scalars(statement.order_by(ScreeningResultItemORM.symbol)))


class AssetScoreRepository:
    """标的透明评分仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_asset_score(
        self,
        *,
        score_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        universe_id: str,
        screening_id: str,
        factor_frame_id: str,
        horizon: str,
        strategy_id: str,
        total_score: Decimal,
        rank: int,
        confidence: Decimal,
        rule_version: str,
        status: str,
        as_of: datetime,
        risk_penalty: Decimal,
        missing_penalty: Decimal,
        technical_score: Decimal | None = None,
        fundamental_score: Decimal | None = None,
        valuation_score: Decimal | None = None,
        flow_score: Decimal | None = None,
        derivatives_score: Decimal | None = None,
        event_score: Decimal | None = None,
        rank_in_universe: int | None = None,
        payload: JsonDict | None = None,
    ) -> AssetScoreORM:
        """按 `score_id` 幂等写入评分结果。"""

        values = {
            "score_id": score_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "universe_id": universe_id,
            "screening_id": screening_id,
            "factor_frame_id": factor_frame_id,
            "horizon": horizon,
            "strategy_id": strategy_id,
            "total_score": total_score,
            "technical_score": technical_score,
            "fundamental_score": fundamental_score,
            "valuation_score": valuation_score,
            "flow_score": flow_score,
            "derivatives_score": derivatives_score,
            "event_score": event_score,
            "risk_penalty": risk_penalty,
            "rank": rank,
            "rank_in_universe": rank_in_universe,
            "confidence": confidence,
            "missing_penalty": missing_penalty,
            "rule_version": rule_version,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssetScoreORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "score_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetScoreORM.score_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssetScoreORM, score_id)

    def list_scores_for_screening(
        self,
        screening_id: str,
        *,
        strategy_id: str | None = None,
    ) -> list[AssetScoreORM]:
        """查询一次初筛对应的评分结果。"""

        statement = select(AssetScoreORM).where(AssetScoreORM.screening_id == screening_id)
        if strategy_id is not None:
            statement = statement.where(AssetScoreORM.strategy_id == strategy_id)
        statement = statement.order_by(AssetScoreORM.rank)
        return list(self.session.scalars(statement))

    def get_latest_score(
        self,
        *,
        asset_id: str,
        horizon: str,
        strategy_id: str | None = None,
    ) -> AssetScoreORM | None:
        """查询单标的最新多维评分。"""

        statement = select(AssetScoreORM).where(
            AssetScoreORM.asset_id == asset_id,
            AssetScoreORM.horizon == horizon,
        )
        if strategy_id is not None:
            statement = statement.where(AssetScoreORM.strategy_id == strategy_id)
        statement = statement.order_by(AssetScoreORM.as_of.desc()).limit(1)
        return self.session.scalars(statement).one_or_none()


class ScoringStrategyRepository:
    """评分策略仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_strategy(
        self,
        *,
        strategy_id: str,
        market: str,
        name: str,
        description: str | None,
        group_weights: JsonDict,
        missing_penalty: JsonDict,
        status: str = "draft",
    ) -> ScoringStrategyORM:
        """按 `strategy_id` 幂等写入评分策略。"""

        values = {
            "strategy_id": strategy_id,
            "market": market,
            "name": name,
            "description": description,
            "group_weights": _json_safe(group_weights),
            "missing_penalty": _json_safe(missing_penalty),
            "status": status,
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(ScoringStrategyORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "strategy_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[ScoringStrategyORM.strategy_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(ScoringStrategyORM, strategy_id)

    def get_strategy(self, strategy_id: str) -> ScoringStrategyORM | None:
        """按 ID 查询评分策略。"""

        return self.session.get(ScoringStrategyORM, strategy_id)

    def get_active_strategy(self, strategy_id: str) -> ScoringStrategyORM | None:
        """按 ID 查询启用中的评分策略。"""

        statement = select(ScoringStrategyORM).where(
            ScoringStrategyORM.strategy_id == strategy_id,
            ScoringStrategyORM.status == "active",
        )
        return self.session.scalars(statement).one_or_none()

    def list_strategies(
        self,
        *,
        market: str | None = None,
        status: str | None = None,
    ) -> list[ScoringStrategyORM]:
        """查询评分策略列表。"""

        statement = select(ScoringStrategyORM)
        if market:
            statement = statement.where(ScoringStrategyORM.market == market)
        if status:
            statement = statement.where(ScoringStrategyORM.status == status)
        return list(
            self.session.scalars(
                statement.order_by(ScoringStrategyORM.market, ScoringStrategyORM.strategy_id)
            )
        )

    def seed_defaults(self, strategies: Sequence[JsonDict]) -> list[ScoringStrategyORM]:
        """幂等写入默认评分策略。"""

        return [
            self.upsert_strategy(
                strategy_id=strategy["strategy_id"],
                market=strategy["market"],
                name=strategy["name"],
                description=strategy.get("description"),
                group_weights=strategy["group_weights"],
                missing_penalty=strategy["missing_penalty"],
                status=strategy.get("status", "active"),
            )
            for strategy in strategies
        ]


class BacktestRepository:
    """轻量回测结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_result(
        self,
        *,
        backtest_id: str,
        market: str,
        strategy_id: str,
        universe_id: str,
        start_at: datetime,
        end_at: datetime,
        rebalance_frequency: str,
        metrics: JsonDict,
        data_versions: JsonDict,
        status: str,
        payload: JsonDict | None = None,
        created_at: datetime | None = None,
    ) -> BacktestResultORM:
        """按 `backtest_id` 幂等写入回测结果。"""

        values = {
            "backtest_id": backtest_id,
            "market": market,
            "strategy_id": strategy_id,
            "universe_id": universe_id,
            "start_at": start_at,
            "end_at": end_at,
            "rebalance_frequency": rebalance_frequency,
            "metrics": _json_safe(metrics),
            "data_versions": _json_safe(data_versions),
            "status": status,
            "created_at": created_at or datetime.now().astimezone(),
            "payload": _json_safe(payload or {}),
        }
        statement = insert(BacktestResultORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "backtest_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[BacktestResultORM.backtest_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(BacktestResultORM, backtest_id)

    def get_latest_result(
        self,
        *,
        market: str,
        strategy_id: str,
        universe_id: str,
        status: str = "available",
    ) -> BacktestResultORM | None:
        """查询指定市场、策略和候选池最近一次可用回测结果。"""

        statement = (
            select(BacktestResultORM)
            .where(
                BacktestResultORM.market == market,
                BacktestResultORM.strategy_id == strategy_id,
                BacktestResultORM.universe_id == universe_id,
                BacktestResultORM.status == status,
            )
            .order_by(BacktestResultORM.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(statement).one_or_none()

    def list_results(
        self,
        *,
        market: str | None = None,
        strategy_id: str | None = None,
        universe_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[BacktestResultORM]:
        """查询最近回测结果，默认按创建时间倒序。"""

        statement = select(BacktestResultORM)
        if market:
            statement = statement.where(BacktestResultORM.market == market)
        if strategy_id:
            statement = statement.where(BacktestResultORM.strategy_id == strategy_id)
        if universe_id:
            statement = statement.where(BacktestResultORM.universe_id == universe_id)
        if status:
            statement = statement.where(BacktestResultORM.status == status)
        return list(
            self.session.scalars(
                statement.order_by(BacktestResultORM.created_at.desc()).limit(limit)
            )
        )


class StrategyObservationRepository:
    """多策略前向观察账本与试运行状态仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_run(
        self,
        *,
        observation_id: str,
        trade_date: date,
        universe_id: str,
        screening_id: str,
        status: str,
        data_versions: JsonDict,
        payload: JsonDict | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> StrategyObservationRunORM:
        """按交易日和候选池幂等写入观察批次头。"""

        now = datetime.now().astimezone()
        values = {
            "observation_id": observation_id,
            "trade_date": trade_date,
            "universe_id": universe_id,
            "screening_id": screening_id,
            "status": status,
            "data_versions": _json_safe(data_versions),
            "payload": _json_safe(payload or {}),
            "created_at": created_at or now,
            "updated_at": updated_at or now,
        }
        statement = insert(StrategyObservationRunORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"observation_id", "trade_date", "universe_id", "created_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    StrategyObservationRunORM.trade_date,
                    StrategyObservationRunORM.universe_id,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(StrategyObservationRunORM, observation_id)

    def upsert_positions(
        self,
        positions: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """幂等写入同一观察批次下各策略的 Top N 仓位。"""

        now = datetime.now().astimezone()
        rows = _dedupe_rows(
            [
                {
                    **item,
                    "payload": _json_safe(item.get("payload") or {}),
                    "created_at": item.get("created_at") or now,
                    "updated_at": item.get("updated_at") or now,
                }
                for item in positions
            ],
            ("observation_id", "strategy_id", "asset_id"),
        )
        if not rows:
            return 0

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(StrategyObservationPositionORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key]
                for key in rows[0]
                if key
                not in {
                    "position_id",
                    "observation_id",
                    "strategy_id",
                    "asset_id",
                    "created_at",
                }
            }
            return statement.on_conflict_do_update(
                index_elements=[
                    StrategyObservationPositionORM.observation_id,
                    StrategyObservationPositionORM.strategy_id,
                    StrategyObservationPositionORM.asset_id,
                ],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def ensure_outcomes(
        self,
        outcomes: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """只创建尚不存在的 5/10/20 日 pending 收益标签。"""

        now = datetime.now().astimezone()
        rows = _dedupe_rows(
            [
                {
                    **item,
                    "payload": _json_safe(item.get("payload") or {}),
                    "created_at": item.get("created_at") or now,
                    "updated_at": item.get("updated_at") or now,
                }
                for item in outcomes
            ],
            ("position_id", "horizon_days"),
        )
        if not rows:
            return 0

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=lambda chunk: insert(StrategyObservationOutcomeORM)
            .values(list(chunk))
            .on_conflict_do_nothing(
                index_elements=[
                    StrategyObservationOutcomeORM.position_id,
                    StrategyObservationOutcomeORM.horizon_days,
                ]
            ),
        )

    def list_due_outcomes(
        self,
        *,
        as_of: date,
        limit: int = 500,
    ) -> list[StrategyObservationOutcomeORM]:
        """查询已经到期但尚未结算的收益标签。"""

        statement = (
            select(StrategyObservationOutcomeORM)
            .where(
                StrategyObservationOutcomeORM.status == "pending",
                StrategyObservationOutcomeORM.due_trade_date.is_not(None),
                StrategyObservationOutcomeORM.due_trade_date <= as_of,
            )
            .order_by(
                StrategyObservationOutcomeORM.due_trade_date,
                StrategyObservationOutcomeORM.outcome_id,
            )
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def mature_outcomes(self, outcomes: Sequence[JsonDict]) -> int:
        """更新已存在的 pending 收益标签，不创建缺少仓位信息的新行。"""

        updated = 0
        for item in _dedupe_rows(list(outcomes), ("outcome_id",)):
            values = {
                key: _json_safe(value) if key == "payload" else value
                for key, value in item.items()
                if key != "outcome_id"
            }
            values["updated_at"] = item.get("updated_at") or datetime.now().astimezone()
            result = self.session.execute(
                update(StrategyObservationOutcomeORM)
                .where(StrategyObservationOutcomeORM.outcome_id == item["outcome_id"])
                .values(**values)
            )
            updated += int(result.rowcount or 0)
        if outcomes:
            self.session.flush()
        return updated

    def get_trial_state(self, strategy_id: str) -> StrategyTrialStateORM | None:
        """读取单个策略的历史/前向验证状态。"""

        return self.session.get(StrategyTrialStateORM, strategy_id)

    def upsert_trial_state(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        state: str,
        historical_evidence_id: str | None,
        forward_metrics: JsonDict,
        consecutive_failure_count: int,
        disabled_reason: str | None,
        last_evaluated_at: datetime | None = None,
        payload: JsonDict | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> StrategyTrialStateORM:
        """幂等写入策略试运行状态，保留首次创建时间。"""

        now = datetime.now().astimezone()
        values = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "state": state,
            "historical_evidence_id": historical_evidence_id,
            "forward_metrics": _json_safe(forward_metrics),
            "consecutive_failure_count": consecutive_failure_count,
            "disabled_reason": disabled_reason,
            "last_evaluated_at": last_evaluated_at,
            "payload": _json_safe(payload or {}),
            "created_at": created_at or now,
            "updated_at": updated_at or now,
        }
        statement = insert(StrategyTrialStateORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"strategy_id", "created_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[StrategyTrialStateORM.strategy_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(StrategyTrialStateORM, strategy_id)

    def list_recent_matured_outcomes(
        self,
        *,
        strategy_id: str,
        horizon_days: int | None = None,
        limit: int = 500,
    ) -> list[StrategyObservationOutcomeORM]:
        """按策略读取最近成熟的前向收益标签。"""

        statement = (
            select(StrategyObservationOutcomeORM)
            .join(
                StrategyObservationPositionORM,
                StrategyObservationPositionORM.position_id
                == StrategyObservationOutcomeORM.position_id,
            )
            .where(
                StrategyObservationPositionORM.strategy_id == strategy_id,
                StrategyObservationOutcomeORM.status == "matured",
            )
        )
        if horizon_days is not None:
            statement = statement.where(
                StrategyObservationOutcomeORM.horizon_days == horizon_days
            )
        return list(
            self.session.scalars(
                statement.order_by(StrategyObservationOutcomeORM.exit_date.desc()).limit(limit)
            )
        )


class SignalSnapshotRepository:
    """信号快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_signal_snapshot(
        self,
        *,
        signal_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        horizon: str,
        direction: str,
        score: Decimal,
        confidence: Decimal,
        rule_version: str,
        status: str,
        as_of: datetime,
        payload: JsonDict | None = None,
    ) -> SignalSnapshotORM:
        """按 `signal_id` 幂等写入信号快照。"""

        values = {
            "signal_id": signal_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "horizon": horizon,
            "direction": direction,
            "score": score,
            "confidence": confidence,
            "rule_version": rule_version,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(SignalSnapshotORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "signal_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[SignalSnapshotORM.signal_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(SignalSnapshotORM, signal_id)

    def get_latest_signal(
        self,
        *,
        asset_id: str,
        horizon: str,
    ) -> SignalSnapshotORM | None:
        """查询单标的最新信号。"""

        statement = select(SignalSnapshotORM).where(
            SignalSnapshotORM.asset_id == asset_id,
            SignalSnapshotORM.horizon == horizon,
        )
        return self.session.scalars(
            statement.order_by(SignalSnapshotORM.as_of.desc()).limit(1)
        ).one_or_none()

    def list_recent_signals(
        self,
        *,
        asset_id: str,
        horizon: str,
        limit: int = 5,
    ) -> list[SignalSnapshotORM]:
        """查询单标的最近信号快照，返回时间倒序结果。"""

        statement = (
            select(SignalSnapshotORM)
            .where(
                SignalSnapshotORM.asset_id == asset_id,
                SignalSnapshotORM.horizon == horizon,
            )
            .order_by(SignalSnapshotORM.as_of.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


class RecommendationRepository:
    """推荐运行和单标的推荐结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_run(
        self,
        *,
        run_id: str,
        strategy: str,
        market: str,
        horizon: str,
        limit: int,
        status: str,
        started_at: datetime,
        universe_id: str | None = None,
        screening_id: str | None = None,
        finished_at: datetime | None = None,
        summary: str | None = None,
        payload: JsonDict | None = None,
    ) -> RecommendationRunORM:
        """按 `run_id` 幂等写入推荐运行。"""

        _ensure_not_mixed_market(market, context="推荐运行")
        values = {
            "run_id": run_id,
            "universe_id": universe_id,
            "screening_id": screening_id,
            "strategy": strategy,
            "market": market,
            "horizon": horizon,
            "limit": limit,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "summary": summary,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(RecommendationRunORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "run_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[RecommendationRunORM.run_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(RecommendationRunORM, run_id)

    def upsert_run_universe(
        self,
        *,
        record_id: str,
        run_id: str,
        universe_id: str,
        market: str,
        role: str,
        weight: Decimal | None = None,
        asset_count: int | None = None,
        payload: JsonDict | None = None,
    ) -> RecommendationRunUniverseORM:
        """按 `run_id + universe_id` 幂等写入推荐运行候选池关联。"""

        _ensure_not_mixed_market(market, context="推荐运行候选池关联")
        run = self.session.get(RecommendationRunORM, run_id)
        if run is not None:
            _ensure_same_market(
                expected=run.market,
                actual=market,
                context=f"推荐运行 {run_id}",
                subject=f"候选池 {universe_id}",
            )
        values = {
            "id": record_id,
            "run_id": run_id,
            "universe_id": universe_id,
            "market": market,
            "role": role,
            "weight": weight,
            "asset_count": asset_count,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(RecommendationRunUniverseORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"id", "run_id", "universe_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_run_universes_run_universe",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_run_universe(run_id=run_id, universe_id=universe_id)

    def upsert_asset_recommendation(
        self,
        *,
        recommendation_id: str,
        run_id: str,
        asset_id: str,
        symbol: str,
        name: str,
        market: str,
        horizon: str,
        action: str,
        rank: int,
        total_score: Decimal,
        confidence: Decimal,
        conviction: str,
        score_id: str | None = None,
        factor_frame_id: str | None = None,
        signal_ids: list[str] | None = None,
        risk_ids: list[str] | None = None,
        agent_analysis_item_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        watch_conditions: JsonDict | None = None,
        invalid_if: JsonDict | None = None,
        summary: str | None = None,
        payload: JsonDict | None = None,
    ) -> AssetRecommendationORM:
        """按 `recommendation_id` 幂等写入单标的推荐结果。"""

        _ensure_not_mixed_market(market, context="推荐结果")
        run = self.session.get(RecommendationRunORM, run_id)
        if run is not None:
            _ensure_same_market(
                expected=run.market,
                actual=market,
                context=f"推荐运行 {run_id}",
                subject=f"推荐标的 {asset_id}",
            )
        values = {
            "recommendation_id": recommendation_id,
            "run_id": run_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "market": market,
            "horizon": horizon,
            "action": action,
            "rank": rank,
            "total_score": total_score,
            "confidence": confidence,
            "conviction": conviction,
            "score_id": score_id,
            "factor_frame_id": factor_frame_id,
            "signal_ids": signal_ids or [],
            "risk_ids": risk_ids or [],
            "agent_analysis_item_ids": agent_analysis_item_ids or [],
            "evidence_ids": evidence_ids or [],
            "watch_conditions": _json_safe(watch_conditions or {}),
            "invalid_if": _json_safe(invalid_if or {}),
            "summary": summary,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssetRecommendationORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key != "recommendation_id"
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetRecommendationORM.recommendation_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssetRecommendationORM, recommendation_id)

    def get_run_universe(
        self,
        *,
        run_id: str,
        universe_id: str,
    ) -> RecommendationRunUniverseORM:
        """查询推荐运行候选池关联。"""

        statement = select(RecommendationRunUniverseORM).where(
            RecommendationRunUniverseORM.run_id == run_id,
            RecommendationRunUniverseORM.universe_id == universe_id,
        )
        return self.session.scalars(statement).one()

    def list_recommendations(self, run_id: str) -> list[AssetRecommendationORM]:
        """查询一次推荐运行的推荐结果。"""

        statement = (
            select(AssetRecommendationORM)
            .where(AssetRecommendationORM.run_id == run_id)
            .order_by(AssetRecommendationORM.rank)
        )
        return list(self.session.scalars(statement))

    def get_recommendation(self, recommendation_id: str) -> AssetRecommendationORM:
        """根据推荐 ID 查询单条推荐结果。"""

        return self.session.get_one(AssetRecommendationORM, recommendation_id)

    def list_top_recommendations(
        self,
        *,
        run_id: str,
        limit: int = 20,
    ) -> list[AssetRecommendationORM]:
        """查询一次推荐运行的前 N 条推荐结果。"""

        statement = (
            select(AssetRecommendationORM)
            .where(AssetRecommendationORM.run_id == run_id)
            .order_by(AssetRecommendationORM.rank)
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_available_runs_since(
        self,
        *,
        since: datetime,
        market: str | None = None,
        limit: int = 20,
        include_smoke: bool = False,
    ) -> list[RecommendationRunORM]:
        """查询最近完成或可用的推荐运行。"""

        statement = select(RecommendationRunORM).where(
            RecommendationRunORM.status == "available",
            RecommendationRunORM.started_at >= since,
        )
        if market:
            statement = statement.where(RecommendationRunORM.market == market)
        if not include_smoke:
            statement = statement.where(
                RecommendationRunORM.run_id.not_ilike("%smoke%"),
                RecommendationRunORM.strategy.not_ilike("%smoke%"),
                func.coalesce(RecommendationRunORM.universe_id, "").not_ilike("%smoke%"),
                RecommendationRunORM.payload.cast(String).not_ilike("%smoke%"),
            )
        runs = list(
            self.session.scalars(
                statement.order_by(RecommendationRunORM.started_at.desc()).limit(limit)
            )
        )
        if include_smoke:
            return runs
        return [run for run in runs if not is_smoke_recommendation_run(run)]


def is_smoke_recommendation_run(run: Any) -> bool:
    """判断推荐运行是否来自冒烟/样例数据。"""

    values = [
        getattr(run, "run_id", None),
        getattr(run, "strategy", None),
        getattr(run, "universe_id", None),
        getattr(run, "summary", None),
        getattr(run, "payload", None),
    ]
    return any("smoke" in str(value).lower() for value in values if value is not None)


class FundamentalDataRepository:
    """A 股财务估值快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_fundamental_snapshot(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        symbol: str,
        source: str,
        status: str,
        as_of: datetime,
        report_period: str | None = None,
        pe_ttm: Decimal | None = None,
        pb: Decimal | None = None,
        roe: Decimal | None = None,
        revenue_growth_yoy: Decimal | None = None,
        net_profit_growth_yoy: Decimal | None = None,
        debt_to_asset: Decimal | None = None,
        operating_cashflow: Decimal | None = None,
        missing_fields: list[str] | None = None,
        payload: JsonDict | None = None,
    ) -> FundamentalSnapshotORM:
        """按 `snapshot_id` 幂等写入财务估值快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "report_period": report_period,
            "pe_ttm": pe_ttm,
            "pb": pb,
            "roe": roe,
            "revenue_growth_yoy": revenue_growth_yoy,
            "net_profit_growth_yoy": net_profit_growth_yoy,
            "debt_to_asset": debt_to_asset,
            "operating_cashflow": operating_cashflow,
            "source": source,
            "status": status,
            "missing_fields": missing_fields or [],
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(FundamentalSnapshotORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "snapshot_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[FundamentalSnapshotORM.snapshot_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(FundamentalSnapshotORM, snapshot_id)

    def upsert_fundamental_snapshots(
        self,
        snapshots: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入财务和估值快照。"""

        rows = _dedupe_rows(
            [
                {
                    "snapshot_id": item["snapshot_id"],
                    "asset_id": item["asset_id"],
                    "symbol": item["symbol"],
                    "report_period": item.get("report_period"),
                    "pe_ttm": item.get("pe_ttm"),
                    "pb": item.get("pb"),
                    "roe": item.get("roe"),
                    "revenue_growth_yoy": item.get("revenue_growth_yoy"),
                    "net_profit_growth_yoy": item.get("net_profit_growth_yoy"),
                    "debt_to_asset": item.get("debt_to_asset"),
                    "operating_cashflow": item.get("operating_cashflow"),
                    "source": item["source"],
                    "status": item["status"],
                    "missing_fields": item.get("missing_fields") or [],
                    "as_of": item["as_of"],
                    "payload": _json_safe(item.get("payload") or {}),
                }
                for item in snapshots
            ],
            ("snapshot_id",),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(FundamentalSnapshotORM).values(list(chunk))
            update_values = {key: statement.excluded[key] for key in rows[0] if key != "snapshot_id"}
            return statement.on_conflict_do_update(
                index_elements=[FundamentalSnapshotORM.snapshot_id],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def get_latest_snapshot(self, *, asset_id: str) -> FundamentalSnapshotORM | None:
        """查询单标的最新财务估值快照。"""

        statement = select(FundamentalSnapshotORM).where(
            FundamentalSnapshotORM.asset_id == asset_id
        )
        return self.session.scalars(
            statement.order_by(FundamentalSnapshotORM.as_of.desc()).limit(1)
        ).one_or_none()

    def list_recent_snapshots(
        self,
        *,
        asset_id: str,
        limit: int,
        source: str | None = None,
    ) -> list[FundamentalSnapshotORM]:
        """查询单标的最近 N 条财务估值快照，返回时间升序结果。"""

        statement = select(FundamentalSnapshotORM).where(
            FundamentalSnapshotORM.asset_id == asset_id
        )
        if source:
            statement = statement.where(FundamentalSnapshotORM.source == source)
        rows = list(
            self.session.scalars(
                statement.order_by(FundamentalSnapshotORM.as_of.desc()).limit(limit)
            )
        )
        return list(reversed(rows))


class PortfolioRepository:
    """私人金融助手组合和持仓仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_portfolio(
        self,
        *,
        portfolio_id: str,
        owner_id: str,
        name: str,
        portfolio_type: str,
        base_currency: str,
        risk_profile: str,
        as_of: datetime,
        total_equity: Decimal | None = None,
        cash: Decimal | None = None,
        market_value: Decimal | None = None,
        max_position_weight: Decimal | None = None,
        max_drawdown_alert: Decimal | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> PortfolioORM:
        """按 `portfolio_id` 幂等写入组合定义。"""

        values = {
            "portfolio_id": portfolio_id,
            "owner_id": owner_id,
            "name": name,
            "portfolio_type": portfolio_type,
            "base_currency": base_currency,
            "risk_profile": risk_profile,
            "total_equity": total_equity,
            "cash": cash,
            "market_value": market_value,
            "max_position_weight": max_position_weight,
            "max_drawdown_alert": max_drawdown_alert,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(PortfolioORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "portfolio_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[PortfolioORM.portfolio_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(PortfolioORM, portfolio_id)

    def insert_portfolio_snapshot(
        self,
        *,
        snapshot_id: str,
        portfolio_id: str,
        owner_id: str,
        captured_at: datetime,
        source: str,
        total_equity: Decimal | None = None,
        cash: Decimal | None = None,
        market_value: Decimal | None = None,
        position_count: int | None = None,
        payload: JsonDict | None = None,
    ) -> PortfolioSnapshotORM:
        """写入组合历史快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "portfolio_id": portfolio_id,
            "owner_id": owner_id,
            "total_equity": total_equity,
            "cash": cash,
            "market_value": market_value,
            "position_count": position_count,
            "source": source,
            "captured_at": captured_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(PortfolioSnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"snapshot_id", "captured_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    PortfolioSnapshotORM.snapshot_id,
                    PortfolioSnapshotORM.captured_at,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            PortfolioSnapshotORM,
            {"snapshot_id": snapshot_id, "captured_at": captured_at},
        )

    def upsert_position(
        self,
        *,
        position_id: str,
        portfolio_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        side: str,
        quantity: Decimal,
        as_of: datetime,
        avg_cost: Decimal | None = None,
        last_price: Decimal | None = None,
        market_value: Decimal | None = None,
        unrealized_pnl: Decimal | None = None,
        unrealized_pnl_pct: Decimal | None = None,
        portfolio_weight: Decimal | None = None,
        leverage: Decimal | None = None,
        liquidation_price: Decimal | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> PositionORM:
        """按 `portfolio_id + asset_id + side` 幂等写入当前持仓。"""

        values = {
            "position_id": position_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "side": side,
            "quantity": quantity,
            "avg_cost": avg_cost,
            "last_price": last_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "portfolio_weight": portfolio_weight,
            "leverage": leverage,
            "liquidation_price": liquidation_price,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(PositionORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"position_id", "portfolio_id", "asset_id", "side"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_positions_portfolio_asset_side",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_position(portfolio_id=portfolio_id, asset_id=asset_id, side=side)

    def insert_position_snapshot(
        self,
        *,
        snapshot_id: str,
        position_id: str,
        portfolio_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        side: str,
        quantity: Decimal,
        captured_at: datetime,
        source: str,
        avg_cost: Decimal | None = None,
        last_price: Decimal | None = None,
        market_value: Decimal | None = None,
        unrealized_pnl: Decimal | None = None,
        unrealized_pnl_pct: Decimal | None = None,
        portfolio_weight: Decimal | None = None,
        leverage: Decimal | None = None,
        liquidation_price: Decimal | None = None,
        payload: JsonDict | None = None,
    ) -> PositionSnapshotORM:
        """写入持仓历史快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "position_id": position_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "side": side,
            "quantity": quantity,
            "avg_cost": avg_cost,
            "last_price": last_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "portfolio_weight": portfolio_weight,
            "leverage": leverage,
            "liquidation_price": liquidation_price,
            "source": source,
            "captured_at": captured_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(PositionSnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"snapshot_id", "captured_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    PositionSnapshotORM.snapshot_id,
                    PositionSnapshotORM.captured_at,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            PositionSnapshotORM,
            {"snapshot_id": snapshot_id, "captured_at": captured_at},
        )

    def get_portfolio(self, portfolio_id: str) -> PortfolioORM:
        """根据组合 ID 查询组合。"""

        return self.session.get_one(PortfolioORM, portfolio_id)

    def list_portfolios(self, *, owner_id: str, status: str | None = None) -> list[PortfolioORM]:
        """查询用户组合。"""

        statement = select(PortfolioORM).where(PortfolioORM.owner_id == owner_id)
        if status:
            statement = statement.where(PortfolioORM.status == status)
        return list(self.session.scalars(statement.order_by(PortfolioORM.updated_at.desc())))

    def get_position(self, *, portfolio_id: str, asset_id: str, side: str) -> PositionORM:
        """根据组合、资产和方向查询持仓。"""

        statement = select(PositionORM).where(
            PositionORM.portfolio_id == portfolio_id,
            PositionORM.asset_id == asset_id,
            PositionORM.side == side,
        )
        return self.session.scalars(statement).one()

    def list_positions(
        self,
        portfolio_id: str,
        *,
        status: str | None = "active",
    ) -> list[PositionORM]:
        """查询组合当前持仓。"""

        statement = select(PositionORM).where(PositionORM.portfolio_id == portfolio_id)
        if status:
            statement = statement.where(PositionORM.status == status)
        return list(
            self.session.scalars(statement.order_by(PositionORM.market, PositionORM.symbol))
        )

    def list_portfolio_snapshots(
        self,
        *,
        portfolio_id: str,
        limit: int = 20,
    ) -> list[PortfolioSnapshotORM]:
        """查询组合历史快照，返回时间倒序结果。"""

        statement = (
            select(PortfolioSnapshotORM)
            .where(PortfolioSnapshotORM.portfolio_id == portfolio_id)
            .order_by(PortfolioSnapshotORM.captured_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_position_snapshots(
        self,
        *,
        portfolio_id: str,
        asset_id: str | None = None,
        limit: int = 20,
    ) -> list[PositionSnapshotORM]:
        """查询持仓历史快照，返回时间倒序结果。"""

        statement = select(PositionSnapshotORM).where(
            PositionSnapshotORM.portfolio_id == portfolio_id
        )
        if asset_id:
            statement = statement.where(PositionSnapshotORM.asset_id == asset_id)
        return list(
            self.session.scalars(
                statement.order_by(PositionSnapshotORM.captured_at.desc()).limit(limit)
            )
        )


class WatchlistRepository:
    """私人观察池和投资假设仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_watchlist(
        self,
        *,
        watchlist_id: str,
        owner_id: str,
        name: str,
        purpose: str,
        market: str | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> WatchlistORM:
        """按 `watchlist_id` 幂等写入观察池。"""

        values = {
            "watchlist_id": watchlist_id,
            "owner_id": owner_id,
            "name": name,
            "market": market,
            "purpose": purpose,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(WatchlistORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "watchlist_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[WatchlistORM.watchlist_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(WatchlistORM, watchlist_id)

    def upsert_watchlist_item(
        self,
        *,
        watchlist_item_id: str,
        watchlist_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        source_type: str,
        reason: str,
        source_id: str | None = None,
        watch_conditions: JsonDict | None = None,
        trigger_conditions: JsonDict | None = None,
        invalid_conditions: JsonDict | None = None,
        risk_level: str | None = None,
        status: str = "active",
        next_review_at: datetime | None = None,
        removed_at: datetime | None = None,
        removed_reason: str | None = None,
        payload: JsonDict | None = None,
    ) -> WatchlistItemORM:
        """按 `watchlist_id + asset_id` 幂等写入观察项。"""

        values = {
            "watchlist_item_id": watchlist_item_id,
            "watchlist_id": watchlist_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "source_type": source_type,
            "source_id": source_id,
            "reason": reason,
            "watch_conditions": _json_safe(watch_conditions or {}),
            "trigger_conditions": _json_safe(trigger_conditions or {}),
            "invalid_conditions": _json_safe(invalid_conditions or {}),
            "risk_level": risk_level,
            "status": status,
            "next_review_at": next_review_at,
            "removed_at": removed_at,
            "removed_reason": removed_reason,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(WatchlistItemORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"watchlist_item_id", "watchlist_id", "asset_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_watchlist_items_watchlist_asset",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_watchlist_item(watchlist_id=watchlist_id, asset_id=asset_id)

    def upsert_asset_thesis(
        self,
        *,
        thesis_id: str,
        asset_id: str,
        owner_id: str,
        source_type: str,
        thesis: str,
        source_id: str | None = None,
        supporting_points: list[JsonDict] | None = None,
        risk_points: list[JsonDict] | None = None,
        invalid_if: JsonDict | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> AssetThesisORM:
        """按 `thesis_id` 幂等写入投资假设。"""

        values = {
            "thesis_id": thesis_id,
            "asset_id": asset_id,
            "owner_id": owner_id,
            "source_type": source_type,
            "source_id": source_id,
            "thesis": thesis,
            "supporting_points": _json_safe(supporting_points or []),
            "risk_points": _json_safe(risk_points or []),
            "invalid_if": _json_safe(invalid_if or {}),
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssetThesisORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "thesis_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetThesisORM.thesis_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssetThesisORM, thesis_id)

    def insert_watchlist_event(
        self,
        *,
        event_id: str,
        owner_id: str,
        watchlist_id: str,
        watchlist_item_id: str,
        asset_id: str,
        event_type: str,
        to_status: str,
        created_at: datetime,
        from_status: str | None = None,
        reason: str | None = None,
        source_decision_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> WatchlistItemEventORM:
        """写入观察池成员事件。"""

        values = {
            "event_id": event_id,
            "owner_id": owner_id,
            "watchlist_id": watchlist_id,
            "watchlist_item_id": watchlist_item_id,
            "asset_id": asset_id,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "source_decision_id": source_decision_id,
            "created_at": created_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(WatchlistItemEventORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "event_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[WatchlistItemEventORM.event_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(WatchlistItemEventORM, event_id)

    def get_watchlist(self, watchlist_id: str) -> WatchlistORM:
        """根据观察池 ID 查询观察池。"""

        return self.session.get_one(WatchlistORM, watchlist_id)

    def get_watchlist_item(self, *, watchlist_id: str, asset_id: str) -> WatchlistItemORM:
        """根据观察池和资产查询观察项。"""

        statement = select(WatchlistItemORM).where(
            WatchlistItemORM.watchlist_id == watchlist_id,
            WatchlistItemORM.asset_id == asset_id,
        )
        return self.session.scalars(statement).one()

    def list_active_items(
        self,
        *,
        owner_id: str,
        watchlist_id: str | None = None,
    ) -> list[WatchlistItemORM]:
        """查询用户所有活跃观察项。"""

        statement = (
            select(WatchlistItemORM)
            .join(WatchlistORM, WatchlistORM.watchlist_id == WatchlistItemORM.watchlist_id)
            .where(WatchlistORM.owner_id == owner_id, WatchlistItemORM.status == "active")
        )
        if watchlist_id:
            statement = statement.where(WatchlistItemORM.watchlist_id == watchlist_id)
        return list(
            self.session.scalars(
                statement.order_by(
                    WatchlistItemORM.next_review_at.asc().nullslast(),
                    WatchlistItemORM.updated_at.desc(),
                )
            )
        )

    def list_asset_theses(
        self,
        *,
        owner_id: str,
        asset_id: str,
        status: str | None = "active",
    ) -> list[AssetThesisORM]:
        """查询单资产投资假设。"""

        statement = select(AssetThesisORM).where(
            AssetThesisORM.owner_id == owner_id,
            AssetThesisORM.asset_id == asset_id,
        )
        if status:
            statement = statement.where(AssetThesisORM.status == status)
        return list(self.session.scalars(statement.order_by(AssetThesisORM.updated_at.desc())))

    def list_watchlist_events(
        self,
        *,
        watchlist_id: str,
        limit: int = 50,
    ) -> list[WatchlistItemEventORM]:
        """查询观察池事件，返回时间倒序结果。"""

        statement = (
            select(WatchlistItemEventORM)
            .where(WatchlistItemEventORM.watchlist_id == watchlist_id)
            .order_by(WatchlistItemEventORM.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_recent_watchlist_item_events(
        self,
        *,
        watchlist_id: str,
        asset_id: str,
        limit: int = 10,
    ) -> list[WatchlistItemEventORM]:
        """按观察池和资产查询最近成员事件，供研究池冷却期判断使用。"""

        statement = (
            select(WatchlistItemEventORM)
            .where(
                WatchlistItemEventORM.watchlist_id == watchlist_id,
                WatchlistItemEventORM.asset_id == asset_id,
            )
            .order_by(WatchlistItemEventORM.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


class AssistantTriggerRepository:
    """私人金融助手触发事件仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_trigger_event(
        self,
        *,
        trigger_event_id: str,
        owner_id: str,
        trigger_type: str,
        dedup_key: str,
        severity: str,
        status: str,
        requested_workflow_type: str,
        triggered_at: datetime,
        trigger_ref: str | None = None,
        agent_runtime: str = "hermes_agent",
        agent_task_id: str | None = None,
        portfolio_id: str | None = None,
        watchlist_id: str | None = None,
        recommendation_run_id: str | None = None,
        asset_id: str | None = None,
        cooldown_until: datetime | None = None,
        dispatched_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> AssistantTriggerEventORM:
        """按触发事件 ID 幂等写入触发事件。"""

        values = {
            "trigger_event_id": trigger_event_id,
            "owner_id": owner_id,
            "trigger_type": trigger_type,
            "trigger_ref": trigger_ref,
            "dedup_key": dedup_key,
            "severity": severity,
            "status": status,
            "agent_runtime": agent_runtime,
            "agent_task_id": agent_task_id,
            "requested_workflow_type": requested_workflow_type,
            "portfolio_id": portfolio_id,
            "watchlist_id": watchlist_id,
            "recommendation_run_id": recommendation_run_id,
            "asset_id": asset_id,
            "cooldown_until": cooldown_until,
            "triggered_at": triggered_at,
            "dispatched_at": dispatched_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssistantTriggerEventORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key != "trigger_event_id"
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssistantTriggerEventORM.trigger_event_id],
                set_=update_values,
            )
        )
        self.session.flush()
        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        self.session.refresh(event)
        return event

    def has_recent_event(
        self,
        *,
        dedup_key: str,
        since: datetime,
        statuses: Sequence[str] = ("pending", "dispatched", "skipped"),
    ) -> bool:
        """判断冷却窗口内是否已有同类触发。"""

        statement = (
            select(func.count())
            .select_from(AssistantTriggerEventORM)
            .where(
                AssistantTriggerEventORM.dedup_key == dedup_key,
                AssistantTriggerEventORM.triggered_at >= since,
                AssistantTriggerEventORM.status.in_(statuses),
            )
        )
        return int(self.session.scalar(statement) or 0) > 0

    def list_pending_events(
        self,
        *,
        owner_id: str | None = None,
        limit: int = 50,
    ) -> list[AssistantTriggerEventORM]:
        """查询待派发触发事件。"""

        statement = select(AssistantTriggerEventORM).where(
            AssistantTriggerEventORM.status == "pending"
        )
        if owner_id:
            statement = statement.where(AssistantTriggerEventORM.owner_id == owner_id)
        return list(
            self.session.scalars(
                statement.order_by(
                    AssistantTriggerEventORM.triggered_at.asc(),
                    AssistantTriggerEventORM.severity.desc(),
                ).limit(limit)
            )
        )

    def list_agent_wakeup_events(
        self,
        *,
        owner_id: str | None = None,
        agent_runtime: str | None = None,
        limit: int = 20,
    ) -> list[AssistantTriggerEventORM]:
        """查询已派发但尚未被内部 Agent Loop 完成处理的唤醒事件。"""

        statement = select(AssistantTriggerEventORM).where(
            AssistantTriggerEventORM.status == "dispatched",
            AssistantTriggerEventORM.agent_task_id.is_not(None),
        )
        if owner_id:
            statement = statement.where(AssistantTriggerEventORM.owner_id == owner_id)
        if agent_runtime:
            statement = statement.where(AssistantTriggerEventORM.agent_runtime == agent_runtime)

        rows = list(
            self.session.scalars(
                statement.order_by(
                    AssistantTriggerEventORM.triggered_at.asc(),
                    AssistantTriggerEventORM.severity.desc(),
                ).limit(max(limit * 5, limit))
            )
        )
        return [
            event
            for event in rows
            if (event.payload or {}).get("agent_loop_status")
            not in {"workflow_completed", "skipped", "failed"}
        ][:limit]

    def get_trigger_event_by_agent_task_id(
        self,
        agent_task_id: str,
    ) -> AssistantTriggerEventORM | None:
        """按 Agent 任务 ID 查询触发事件。"""

        statement = select(AssistantTriggerEventORM).where(
            AssistantTriggerEventORM.agent_task_id == agent_task_id
        )
        return self.session.scalars(statement).one_or_none()

    def mark_dispatched(
        self,
        *,
        trigger_event_id: str,
        agent_task_id: str,
        dispatched_at: datetime,
        agent_runtime: str | None = None,
        payload: JsonDict | None = None,
    ) -> AssistantTriggerEventORM:
        """标记触发事件已经派发到 Agent 唤醒队列。"""

        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        merged_payload = dict(event.payload or {})
        merged_payload.update(_json_safe(payload or {}))
        return self.upsert_trigger_event(
            trigger_event_id=event.trigger_event_id,
            owner_id=event.owner_id,
            trigger_type=event.trigger_type,
            trigger_ref=event.trigger_ref,
            dedup_key=event.dedup_key,
            severity=event.severity,
            status="dispatched",
            agent_runtime=agent_runtime or event.agent_runtime,
            agent_task_id=agent_task_id,
            requested_workflow_type=event.requested_workflow_type,
            portfolio_id=event.portfolio_id,
            watchlist_id=event.watchlist_id,
            recommendation_run_id=event.recommendation_run_id,
            asset_id=event.asset_id,
            cooldown_until=event.cooldown_until,
            triggered_at=event.triggered_at,
            dispatched_at=dispatched_at,
            payload=merged_payload,
        )

    def mark_skipped(
        self,
        *,
        trigger_event_id: str,
        skipped_at: datetime,
        reason: str,
    ) -> AssistantTriggerEventORM:
        """标记触发事件被跳过。"""

        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        payload = dict(event.payload or {})
        payload["skip_reason"] = reason
        return self.upsert_trigger_event(
            trigger_event_id=event.trigger_event_id,
            owner_id=event.owner_id,
            trigger_type=event.trigger_type,
            trigger_ref=event.trigger_ref,
            dedup_key=event.dedup_key,
            severity=event.severity,
            status="skipped",
            agent_runtime=event.agent_runtime,
            agent_task_id=event.agent_task_id,
            requested_workflow_type=event.requested_workflow_type,
            portfolio_id=event.portfolio_id,
            watchlist_id=event.watchlist_id,
            recommendation_run_id=event.recommendation_run_id,
            asset_id=event.asset_id,
            cooldown_until=event.cooldown_until,
            triggered_at=event.triggered_at,
            dispatched_at=skipped_at,
            payload=payload,
        )

    def mark_agent_loop_completed(
        self,
        *,
        trigger_event_id: str,
        workflow_run_id: str,
        completed_at: datetime,
        payload: JsonDict | None = None,
    ) -> AssistantTriggerEventORM:
        """标记触发事件已经被内部 Agent Loop 处理完成。"""

        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        merged_payload = dict(event.payload or {})
        merged_payload.update(
            {
                "agent_loop_status": "workflow_completed",
                "handled_by": "InternalFinanceAgentLoop",
                "handled_at": completed_at.isoformat(),
                "workflow_run_id": workflow_run_id,
            }
        )
        merged_payload.update(_json_safe(payload or {}))
        return self.upsert_trigger_event(
            trigger_event_id=event.trigger_event_id,
            owner_id=event.owner_id,
            trigger_type=event.trigger_type,
            trigger_ref=event.trigger_ref,
            dedup_key=event.dedup_key,
            severity=event.severity,
            status=event.status,
            agent_runtime=event.agent_runtime,
            agent_task_id=event.agent_task_id,
            requested_workflow_type=event.requested_workflow_type,
            portfolio_id=event.portfolio_id,
            watchlist_id=event.watchlist_id,
            recommendation_run_id=event.recommendation_run_id,
            asset_id=event.asset_id,
            cooldown_until=event.cooldown_until,
            triggered_at=event.triggered_at,
            dispatched_at=event.dispatched_at,
            payload=merged_payload,
        )

    def mark_agent_loop_skipped(
        self,
        *,
        trigger_event_id: str,
        skipped_at: datetime,
        reason: str,
        payload: JsonDict | None = None,
    ) -> AssistantTriggerEventORM:
        """标记触发事件被内部 Agent Loop 跳过。"""

        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        merged_payload = dict(event.payload or {})
        merged_payload.update(
            {
                "agent_loop_status": "skipped",
                "handled_by": "InternalFinanceAgentLoop",
                "handled_at": skipped_at.isoformat(),
                "skip_reason": reason,
            }
        )
        merged_payload.update(_json_safe(payload or {}))
        return self.upsert_trigger_event(
            trigger_event_id=event.trigger_event_id,
            owner_id=event.owner_id,
            trigger_type=event.trigger_type,
            trigger_ref=event.trigger_ref,
            dedup_key=event.dedup_key,
            severity=event.severity,
            status=event.status,
            agent_runtime=event.agent_runtime,
            agent_task_id=event.agent_task_id,
            requested_workflow_type=event.requested_workflow_type,
            portfolio_id=event.portfolio_id,
            watchlist_id=event.watchlist_id,
            recommendation_run_id=event.recommendation_run_id,
            asset_id=event.asset_id,
            cooldown_until=event.cooldown_until,
            triggered_at=event.triggered_at,
            dispatched_at=event.dispatched_at,
            payload=merged_payload,
        )

    def mark_agent_loop_failed(
        self,
        *,
        trigger_event_id: str,
        failed_at: datetime,
        error_message: str,
        payload: JsonDict | None = None,
    ) -> AssistantTriggerEventORM:
        """标记触发事件被内部 Agent Loop 处理失败。"""

        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        merged_payload = dict(event.payload or {})
        merged_payload.update(
            {
                "agent_loop_status": "failed",
                "handled_by": "InternalFinanceAgentLoop",
                "handled_at": failed_at.isoformat(),
                "error_message": error_message,
            }
        )
        merged_payload.update(_json_safe(payload or {}))
        return self.upsert_trigger_event(
            trigger_event_id=event.trigger_event_id,
            owner_id=event.owner_id,
            trigger_type=event.trigger_type,
            trigger_ref=event.trigger_ref,
            dedup_key=event.dedup_key,
            severity=event.severity,
            status=event.status,
            agent_runtime=event.agent_runtime,
            agent_task_id=event.agent_task_id,
            requested_workflow_type=event.requested_workflow_type,
            portfolio_id=event.portfolio_id,
            watchlist_id=event.watchlist_id,
            recommendation_run_id=event.recommendation_run_id,
            asset_id=event.asset_id,
            cooldown_until=event.cooldown_until,
            triggered_at=event.triggered_at,
            dispatched_at=event.dispatched_at,
            payload=merged_payload,
        )


class DecisionLogRepository:
    """提醒和决策日志仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_monitoring_alert(
        self,
        *,
        alert_id: str,
        owner_id: str,
        alert_type: str,
        severity: str,
        triggered_by: str,
        trigger_condition: str,
        status: str,
        as_of: datetime,
        portfolio_id: str | None = None,
        asset_id: str | None = None,
        current_value: Decimal | None = None,
        threshold_value: Decimal | None = None,
        payload: JsonDict | None = None,
    ) -> MonitoringAlertORM:
        """写入或覆盖一条监控提醒。"""

        values = {
            "alert_id": alert_id,
            "owner_id": owner_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "alert_type": alert_type,
            "severity": severity,
            "triggered_by": triggered_by,
            "trigger_condition": trigger_condition,
            "current_value": current_value,
            "threshold_value": threshold_value,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(MonitoringAlertORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "alert_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[MonitoringAlertORM.alert_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(MonitoringAlertORM, alert_id)

    def insert_decision_log(
        self,
        *,
        decision_id: str,
        owner_id: str,
        decision_type: str,
        suggested_action: str,
        user_action: str,
        summary: str,
        portfolio_id: str | None = None,
        asset_id: str | None = None,
        source_recommendation_id: str | None = None,
        source_alert_id: str | None = None,
        workflow_run_id: str | None = None,
        reason_ids: list[str] | None = None,
        risk_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        created_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> DecisionLogORM:
        """写入或覆盖一条决策日志。"""

        values = {
            "decision_id": decision_id,
            "owner_id": owner_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "decision_type": decision_type,
            "source_recommendation_id": source_recommendation_id,
            "source_alert_id": source_alert_id,
            "workflow_run_id": workflow_run_id,
            "suggested_action": suggested_action,
            "user_action": user_action,
            "summary": summary,
            "reason_ids": reason_ids or [],
            "risk_ids": risk_ids or [],
            "evidence_ids": evidence_ids or [],
            "created_at": created_at or datetime.now().astimezone(),
            "payload": _json_safe(payload or {}),
        }
        statement = insert(DecisionLogORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "decision_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[DecisionLogORM.decision_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(DecisionLogORM, decision_id)

    def list_recent_decisions(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 20,
    ) -> list[DecisionLogORM]:
        """查询最近决策日志。"""

        statement = select(DecisionLogORM).where(DecisionLogORM.owner_id == owner_id)
        if asset_id:
            statement = statement.where(DecisionLogORM.asset_id == asset_id)
        return list(
            self.session.scalars(statement.order_by(DecisionLogORM.created_at.desc()).limit(limit))
        )


class ActionLoopRepository:
    """人工确认、订单草案和执行登记仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_order_draft(
        self,
        *,
        order_draft_id: str,
        owner_id: str,
        portfolio_id: str,
        asset_id: str,
        market: str,
        decision_log_id: str,
        action: str,
        suggested_price_range: JsonDict | None = None,
        suggested_position_ratio: Decimal | None = None,
        constraints: JsonDict | None = None,
        status: str = "drafted",
        disclaimer: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> OrderDraftORM:
        """按 `order_draft_id` 幂等写入订单草案。"""

        now = datetime.now().astimezone()
        values = {
            "order_draft_id": order_draft_id,
            "owner_id": owner_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "market": market,
            "decision_log_id": decision_log_id,
            "action": action,
            "suggested_price_range": _json_safe(suggested_price_range or {}),
            "suggested_position_ratio": suggested_position_ratio,
            "constraints": _json_safe(constraints or {}),
            "status": status,
            "disclaimer": disclaimer or ACTION_LOOP_DISCLAIMER,
            "created_at": created_at or now,
            "updated_at": updated_at or created_at or now,
        }
        if not str(values["disclaimer"]).strip():
            raise ValueError("订单草案 disclaimer 不能为空")
        statement = insert(OrderDraftORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"order_draft_id", "created_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[OrderDraftORM.order_draft_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(OrderDraftORM, order_draft_id)

    def get_order_draft(self, order_draft_id: str) -> OrderDraftORM | None:
        """按 ID 查询订单草案。"""

        return self.session.get(OrderDraftORM, order_draft_id)

    def list_order_drafts(
        self,
        *,
        owner_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[OrderDraftORM]:
        """查询用户订单草案列表。"""

        statement = select(OrderDraftORM).where(OrderDraftORM.owner_id == owner_id)
        if status:
            statement = statement.where(OrderDraftORM.status == status)
        return list(
            self.session.scalars(statement.order_by(OrderDraftORM.created_at.desc()).limit(limit))
        )

    def supersede_active_order_drafts(
        self,
        *,
        decision_log_id: str,
        superseded_at: datetime | None = None,
    ) -> int:
        """把同一决策下仍处于 drafted 的旧草案标记为 superseded。"""

        changed_at = superseded_at or datetime.now().astimezone()
        statement = select(OrderDraftORM).where(
            OrderDraftORM.decision_log_id == decision_log_id,
            OrderDraftORM.status == "drafted",
        )
        drafts = list(self.session.scalars(statement))
        for draft in drafts:
            draft.status = "superseded"
            draft.updated_at = changed_at
        self.session.flush()
        return len(drafts)

    def upsert_execution_record(
        self,
        *,
        execution_id: str,
        owner_id: str,
        portfolio_id: str,
        asset_id: str,
        market: str,
        action: str,
        executed_price: Decimal,
        executed_quantity: Decimal,
        executed_at: datetime,
        order_draft_id: str | None = None,
        decision_log_id: str | None = None,
        fee: Decimal | None = None,
        note: str | None = None,
        source: str = "user_reported",
        created_at: datetime | None = None,
    ) -> ExecutionRecordORM:
        """按 `execution_id` 幂等写入用户手工执行登记。"""

        if source != "user_reported":
            raise ValueError("执行登记 source 当前只能为 user_reported")
        values = {
            "execution_id": execution_id,
            "owner_id": owner_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "market": market,
            "order_draft_id": order_draft_id,
            "decision_log_id": decision_log_id,
            "action": action,
            "executed_price": executed_price,
            "executed_quantity": executed_quantity,
            "executed_at": executed_at,
            "fee": fee,
            "note": note,
            "source": source,
            "created_at": created_at or datetime.now().astimezone(),
        }
        statement = insert(ExecutionRecordORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"execution_id", "created_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[ExecutionRecordORM.execution_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(ExecutionRecordORM, execution_id)

    def get_execution_record(self, execution_id: str) -> ExecutionRecordORM | None:
        """按 ID 查询执行登记。"""

        return self.session.get(ExecutionRecordORM, execution_id)

    def list_execution_records(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 50,
    ) -> list[ExecutionRecordORM]:
        """查询用户执行登记列表。"""

        statement = select(ExecutionRecordORM).where(ExecutionRecordORM.owner_id == owner_id)
        if asset_id:
            statement = statement.where(ExecutionRecordORM.asset_id == asset_id)
        return list(
            self.session.scalars(
                statement.order_by(ExecutionRecordORM.executed_at.desc()).limit(limit)
            )
        )


class MemoryRepository:
    """Finance Memory、向量索引、轻量图谱和复盘任务仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_memory(
        self,
        *,
        memory_id: str,
        owner_id: str,
        memory_type: str,
        scope: str,
        content: str,
        confidence: Decimal,
        asset_id: str | None = None,
        source_decision_id: str | None = None,
        source_review_task_id: str | None = None,
        embedding_ref: str | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> AssistantMemoryORM:
        """按 `memory_id` 幂等写入 Finance Memory。"""

        values = {
            "memory_id": memory_id,
            "owner_id": owner_id,
            "memory_type": memory_type,
            "scope": scope,
            "asset_id": asset_id,
            "source_decision_id": source_decision_id,
            "source_review_task_id": source_review_task_id,
            "content": content,
            "embedding_ref": embedding_ref,
            "confidence": confidence,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssistantMemoryORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "memory_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssistantMemoryORM.memory_id],
                set_=update_values,
            )
        )
        self.session.flush()
        memory = self.session.get_one(AssistantMemoryORM, memory_id)
        self.session.refresh(memory)
        return memory

    def get_memory(self, memory_id: str) -> AssistantMemoryORM | None:
        """按 ID 查询单条 Finance Memory。"""

        return self.session.get(AssistantMemoryORM, memory_id)

    def get_review_task(self, review_task_id: str) -> ReviewTaskORM | None:
        """按 ID 查询复盘任务。"""

        return self.session.get(ReviewTaskORM, review_task_id)

    def upsert_embedding(
        self,
        *,
        embedding_id: str,
        owner_id: str,
        source_type: str,
        source_id: str,
        chunk_text: str,
        embedding_model: str,
        memory_id: str | None = None,
        embedding: list[float] | None = None,
        content_hash: str | None = None,
        payload: JsonDict | None = None,
    ) -> MemoryEmbeddingORM:
        """按 `embedding_id` 幂等写入 Finance Memory 语义索引。"""

        values = {
            "embedding_id": embedding_id,
            "owner_id": owner_id,
            "memory_id": memory_id,
            "source_type": source_type,
            "source_id": source_id,
            "chunk_text": chunk_text,
            "embedding": _json_safe(embedding) if embedding is not None else None,
            "embedding_model": embedding_model,
            "content_hash": content_hash or _stable_json_hash({"text": chunk_text}),
            "payload": _json_safe(payload or {}),
        }
        statement = insert(MemoryEmbeddingORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "embedding_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[MemoryEmbeddingORM.embedding_id],
                set_=update_values,
            )
        )
        self.session.flush()
        embedding_row = self.session.get_one(MemoryEmbeddingORM, embedding_id)
        self.session.refresh(embedding_row)
        return embedding_row

    def list_embeddings(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 200,
    ) -> list[tuple[MemoryEmbeddingORM, AssistantMemoryORM | None]]:
        """查询 Finance Memory 语义索引，并尽量带回对应记忆。"""

        statement = (
            select(MemoryEmbeddingORM, AssistantMemoryORM)
            .outerjoin(
                AssistantMemoryORM,
                MemoryEmbeddingORM.memory_id == AssistantMemoryORM.memory_id,
            )
            .where(MemoryEmbeddingORM.owner_id == owner_id)
        )
        if asset_id:
            statement = statement.where(
                (AssistantMemoryORM.asset_id == asset_id) | (AssistantMemoryORM.memory_id.is_(None))
            )
        if memory_type:
            statement = statement.where(
                (AssistantMemoryORM.memory_type == memory_type)
                | (AssistantMemoryORM.memory_id.is_(None))
            )
        rows = self.session.execute(
            statement.order_by(MemoryEmbeddingORM.created_at.desc()).limit(limit)
        ).all()
        return [(embedding, memory) for embedding, memory in rows]

    def upsert_edge(
        self,
        *,
        edge_id: str,
        owner_id: str,
        source_type: str,
        source_id: str,
        relation_type: str,
        target_type: str,
        target_id: str,
        confidence: Decimal,
        reason: str | None = None,
        payload: JsonDict | None = None,
    ) -> FinancialMemoryEdgeORM:
        """按语义边唯一约束幂等写入轻量图谱关系。"""

        values = {
            "edge_id": edge_id,
            "owner_id": owner_id,
            "source_type": source_type,
            "source_id": source_id,
            "relation_type": relation_type,
            "target_type": target_type,
            "target_id": target_id,
            "confidence": confidence,
            "reason": reason,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(FinancialMemoryEdgeORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key
            not in {
                "edge_id",
                "owner_id",
                "source_type",
                "source_id",
                "relation_type",
                "target_type",
                "target_id",
            }
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_memory_edges_owner_source_relation_target",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_edge(
            owner_id=owner_id,
            source_type=source_type,
            source_id=source_id,
            relation_type=relation_type,
            target_type=target_type,
            target_id=target_id,
        )

    def upsert_review_task(
        self,
        *,
        review_task_id: str,
        owner_id: str,
        review_type: str,
        due_at: datetime,
        status: str,
        asset_id: str | None = None,
        source_decision_id: str | None = None,
        review_questions: list[JsonDict] | None = None,
        result_summary: str | None = None,
        finished_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> ReviewTaskORM:
        """按 `review_task_id` 幂等写入复盘任务。"""

        values = {
            "review_task_id": review_task_id,
            "owner_id": owner_id,
            "asset_id": asset_id,
            "source_decision_id": source_decision_id,
            "review_type": review_type,
            "due_at": due_at,
            "status": status,
            "review_questions": _json_safe(review_questions or []),
            "result_summary": result_summary,
            "finished_at": finished_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(ReviewTaskORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "review_task_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[ReviewTaskORM.review_task_id],
                set_=update_values,
            )
        )
        self.session.flush()
        task = self.session.get_one(ReviewTaskORM, review_task_id)
        self.session.refresh(task)
        return task

    def get_edge(
        self,
        *,
        owner_id: str,
        source_type: str,
        source_id: str,
        relation_type: str,
        target_type: str,
        target_id: str,
    ) -> FinancialMemoryEdgeORM:
        """根据语义边唯一键查询关系。"""

        statement = select(FinancialMemoryEdgeORM).where(
            FinancialMemoryEdgeORM.owner_id == owner_id,
            FinancialMemoryEdgeORM.source_type == source_type,
            FinancialMemoryEdgeORM.source_id == source_id,
            FinancialMemoryEdgeORM.relation_type == relation_type,
            FinancialMemoryEdgeORM.target_type == target_type,
            FinancialMemoryEdgeORM.target_id == target_id,
        )
        return self.session.scalars(statement).one()

    def list_active_memories(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> list[AssistantMemoryORM]:
        """查询可用 Finance Memory。"""

        statement = select(AssistantMemoryORM).where(
            AssistantMemoryORM.owner_id == owner_id,
            AssistantMemoryORM.status == "active",
        )
        if asset_id:
            statement = statement.where(AssistantMemoryORM.asset_id == asset_id)
        if memory_type:
            statement = statement.where(AssistantMemoryORM.memory_type == memory_type)
        return list(
            self.session.scalars(
                statement.order_by(AssistantMemoryORM.updated_at.desc()).limit(limit)
            )
        )

    def list_memories(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        memory_type: str | None = None,
        statuses: Sequence[str] | None = ("active",),
        limit: int = 20,
    ) -> list[AssistantMemoryORM]:
        """按资产、类型和状态查询 Finance Memory。"""

        statement = select(AssistantMemoryORM).where(AssistantMemoryORM.owner_id == owner_id)
        if asset_id:
            statement = statement.where(AssistantMemoryORM.asset_id == asset_id)
        if memory_type:
            statement = statement.where(AssistantMemoryORM.memory_type == memory_type)
        if statuses is not None:
            statement = statement.where(AssistantMemoryORM.status.in_(tuple(statuses)))
        return list(
            self.session.scalars(
                statement.order_by(
                    AssistantMemoryORM.updated_at.desc(),
                    AssistantMemoryORM.confidence.desc(),
                ).limit(limit)
            )
        )

    def list_memories_by_source_decision(
        self,
        *,
        owner_id: str,
        source_decision_id: str,
        asset_id: str | None = None,
        statuses: Sequence[str] | None = ("active",),
        limit: int = 20,
    ) -> list[AssistantMemoryORM]:
        """查询由同一决策沉淀出来的 Finance Memory。"""

        statement = select(AssistantMemoryORM).where(
            AssistantMemoryORM.owner_id == owner_id,
            AssistantMemoryORM.source_decision_id == source_decision_id,
        )
        if asset_id:
            statement = statement.where(AssistantMemoryORM.asset_id == asset_id)
        if statuses is not None:
            statement = statement.where(AssistantMemoryORM.status.in_(tuple(statuses)))
        return list(
            self.session.scalars(
                statement.order_by(
                    AssistantMemoryORM.updated_at.desc(),
                    AssistantMemoryORM.confidence.desc(),
                ).limit(limit)
            )
        )


DEFAULT_PROFILE_STYLE_TENDENCY = {"value": 0.6, "theme": 0.4}
DEFAULT_PROFILE_CONFIDENCE = {
    "risk_appetite": 0.1,
    "horizon": 0.1,
    "capital_scale": 0.1,
    "style_tendency": 0.1,
    "timing_posture": 0.1,
}
DEFAULT_PROFILE_SOURCE = {
    "risk_appetite": "default",
    "horizon": "default",
    "capital_scale": "default",
    "style_tendency": "default",
    "timing_posture": "default",
}


class UserInvestmentProfileRepository:
    """用户投资画像仓储，负责结构化画像与 Finance Memory 审计同步。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def profile_id_for(owner_id: str) -> str:
        """按用户 ID 生成稳定画像主键。"""

        return f"profile:{owner_id}"

    @staticmethod
    def memory_id_for(owner_id: str) -> str:
        """按用户 ID 生成画像审计记忆主键。"""

        return f"memory:investment_profile:{owner_id}"

    def get_profile(self, *, owner_id: str) -> UserInvestmentProfileORM | None:
        """读取已落库画像。"""

        return self.session.get(UserInvestmentProfileORM, self.profile_id_for(owner_id))

    def get_or_default(self, *, owner_id: str) -> UserInvestmentProfileORM:
        """读取画像；冷启动时返回不落库的默认画像。"""

        profile = self.get_profile(owner_id=owner_id)
        if profile is not None:
            return profile
        now = datetime.now().astimezone()
        return UserInvestmentProfileORM(
            profile_id=self.profile_id_for(owner_id),
            owner_id=owner_id,
            risk_appetite="balanced",
            horizon="swing",
            capital_scale="unknown",
            style_tendency=dict(DEFAULT_PROFILE_STYLE_TENDENCY),
            timing_posture="neutral",
            dimension_confidence=dict(DEFAULT_PROFILE_CONFIDENCE),
            source=dict(DEFAULT_PROFILE_SOURCE),
            status="active",
            created_at=now,
            updated_at=now,
            payload={"status": "default"},
        )

    def upsert_profile(
        self,
        *,
        owner_id: str,
        risk_appetite: str | None = None,
        horizon: str | None = None,
        capital_scale: str | None = None,
        style_tendency: JsonDict | None = None,
        timing_posture: str | None = None,
        source: JsonDict | None = None,
        evidence: Sequence[JsonDict] | None = None,
        confidence_delta: Decimal = Decimal("0.20"),
        updated_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> UserInvestmentProfileORM:
        """幂等写入画像，并同步一条 investment_profile Finance Memory。"""

        now = updated_at or datetime.now().astimezone()
        current = self.get_or_default(owner_id=owner_id)
        changed_dimensions = self._changed_dimensions(
            risk_appetite=risk_appetite,
            horizon=horizon,
            capital_scale=capital_scale,
            style_tendency=style_tendency,
            timing_posture=timing_posture,
            source=source,
        )
        confidence = self._merge_confidence(
            current.dimension_confidence,
            changed_dimensions=changed_dimensions,
            confidence_delta=confidence_delta,
        )
        merged_source = dict(current.source or DEFAULT_PROFILE_SOURCE)
        for key, value in (source or {}).items():
            if value:
                merged_source[key] = value
        profile_payload = {
            **(current.payload or {}),
            **(payload or {}),
            "evidence": _json_safe(list(evidence or [])),
            "updated_by": "profile.upsert",
        }
        values = {
            "profile_id": self.profile_id_for(owner_id),
            "owner_id": owner_id,
            "risk_appetite": risk_appetite or current.risk_appetite,
            "horizon": horizon or current.horizon,
            "capital_scale": capital_scale or current.capital_scale,
            "style_tendency": _json_safe(style_tendency or current.style_tendency),
            "timing_posture": timing_posture or current.timing_posture,
            "dimension_confidence": _json_safe(confidence),
            "source": _json_safe(merged_source),
            "status": "active",
            "updated_at": now,
            "payload": _json_safe(profile_payload),
        }
        statement = insert(UserInvestmentProfileORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "profile_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[UserInvestmentProfileORM.profile_id],
                set_=update_values,
            )
        )
        self.session.flush()
        profile = self.session.get_one(UserInvestmentProfileORM, values["profile_id"])
        self.session.refresh(profile)
        self._upsert_profile_memory(profile=profile, evidence=list(evidence or []))
        return profile

    def apply_confidence_decay(
        self,
        *,
        owner_id: str,
        as_of: datetime | None = None,
        half_life_days: int = 90,
        stale_threshold: Decimal = Decimal("0.05"),
    ) -> UserInvestmentProfileORM | None:
        """按半衰期衰减画像置信度，低于阈值时标记 stale。"""

        profile = self.get_profile(owner_id=owner_id)
        if profile is None:
            return None
        now = as_of or datetime.now().astimezone()
        elapsed_days = max((now - profile.updated_at).total_seconds() / 86400, 0)
        factor = 0.5 ** (elapsed_days / max(half_life_days, 1))
        decayed = {
            key: round(float(value) * factor, 6)
            for key, value in (profile.dimension_confidence or {}).items()
        }
        max_confidence = max(decayed.values(), default=0.0)
        status = "stale" if Decimal(str(max_confidence)) < stale_threshold else "active"
        values = {
            "profile_id": profile.profile_id,
            "owner_id": profile.owner_id,
            "risk_appetite": profile.risk_appetite,
            "horizon": profile.horizon,
            "capital_scale": profile.capital_scale,
            "style_tendency": _json_safe(profile.style_tendency),
            "timing_posture": profile.timing_posture,
            "dimension_confidence": _json_safe(decayed),
            "source": _json_safe(profile.source),
            "status": status,
            "updated_at": now,
            "payload": _json_safe({**(profile.payload or {}), "confidence_decayed_at": now.isoformat()}),
        }
        statement = insert(UserInvestmentProfileORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "profile_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[UserInvestmentProfileORM.profile_id],
                set_=update_values,
            )
        )
        self.session.flush()
        refreshed = self.session.get_one(UserInvestmentProfileORM, profile.profile_id)
        self.session.refresh(refreshed)
        return refreshed

    @staticmethod
    def _changed_dimensions(
        *,
        risk_appetite: str | None,
        horizon: str | None,
        capital_scale: str | None,
        style_tendency: JsonDict | None,
        timing_posture: str | None,
        source: JsonDict | None,
    ) -> set[str]:
        changed = {
            key
            for key, value in {
                "risk_appetite": risk_appetite,
                "horizon": horizon,
                "capital_scale": capital_scale,
                "style_tendency": style_tendency,
                "timing_posture": timing_posture,
            }.items()
            if value is not None
        }
        changed.update(key for key, value in (source or {}).items() if value)
        return changed

    @staticmethod
    def _merge_confidence(
        current: JsonDict | None,
        *,
        changed_dimensions: set[str],
        confidence_delta: Decimal,
    ) -> JsonDict:
        merged = dict(DEFAULT_PROFILE_CONFIDENCE)
        merged.update(current or {})
        delta = float(confidence_delta)
        for dimension in changed_dimensions:
            merged[dimension] = round(min(float(merged.get(dimension, 0.1)) + delta, 1.0), 6)
        return merged

    def _upsert_profile_memory(
        self,
        *,
        profile: UserInvestmentProfileORM,
        evidence: Sequence[JsonDict],
    ) -> None:
        confidence_values = [
            Decimal(str(value)) for value in (profile.dimension_confidence or {}).values()
        ]
        confidence = max(confidence_values, default=Decimal("0.10"))
        values = {
            "memory_id": self.memory_id_for(profile.owner_id),
            "owner_id": profile.owner_id,
            "memory_type": "investment_profile",
            "scope": "owner",
            "asset_id": None,
            "source_decision_id": self._first_evidence_id(evidence, "decision"),
            "source_review_task_id": self._first_evidence_id(evidence, "review"),
            "content": (
                f"用户投资画像：风险偏好={profile.risk_appetite}，"
                f"周期={profile.horizon}，择时={profile.timing_posture}。"
            ),
            "embedding_ref": None,
            "confidence": confidence,
            "status": profile.status,
            "payload": _json_safe(
                {
                    "profile_id": profile.profile_id,
                    "risk_appetite": profile.risk_appetite,
                    "horizon": profile.horizon,
                    "capital_scale": profile.capital_scale,
                    "style_tendency": profile.style_tendency,
                    "timing_posture": profile.timing_posture,
                    "dimension_confidence": profile.dimension_confidence,
                    "source": profile.source,
                    "evidence": list(evidence),
                }
            ),
            "updated_at": profile.updated_at,
        }
        statement = insert(AssistantMemoryORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "memory_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssistantMemoryORM.memory_id],
                set_=update_values,
            )
        )
        self.session.flush()

    @staticmethod
    def _first_evidence_id(evidence: Sequence[JsonDict], evidence_type: str) -> str | None:
        for item in evidence:
            if item.get("type") == evidence_type and item.get("id"):
                return str(item["id"])
        return None


class ChatMemoryRepository:
    """CLI 聊天会话和消息仓储。

    这里保存普通聊天流水，用于恢复上下文；可审计的金融长期记忆仍由
    `MemoryRepository` 写入 `assistant_memories`。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_session(
        self,
        *,
        chat_session_id: str,
        owner_id: str,
        title: str | None = None,
        status: str = "active",
        started_at: datetime | None = None,
        last_message_at: datetime | None = None,
        message_count: int = 0,
        summary: str | None = None,
        payload: JsonDict | None = None,
    ) -> AssistantChatSessionORM:
        """按 `chat_session_id` 幂等写入聊天会话。"""

        now = datetime.now().astimezone()
        values = {
            "chat_session_id": chat_session_id,
            "owner_id": owner_id,
            "title": title,
            "status": status,
            "started_at": started_at or now,
            "last_message_at": last_message_at,
            "message_count": message_count,
            "summary": summary,
            "payload": _json_safe(payload or {}),
            "updated_at": now,
        }
        statement = insert(AssistantChatSessionORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"chat_session_id", "owner_id", "started_at", "created_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssistantChatSessionORM.chat_session_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssistantChatSessionORM, chat_session_id)

    def get_session(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
    ) -> AssistantChatSessionORM | None:
        """按用户和会话 ID 查询聊天会话。"""

        statement = select(AssistantChatSessionORM).where(
            AssistantChatSessionORM.owner_id == owner_id,
            AssistantChatSessionORM.chat_session_id == chat_session_id,
        )
        return self.session.scalars(statement).one_or_none()

    def get_latest_session(self, *, owner_id: str) -> AssistantChatSessionORM | None:
        """查询用户最近活跃的聊天会话。"""

        statement = (
            select(AssistantChatSessionORM)
            .where(
                AssistantChatSessionORM.owner_id == owner_id,
                AssistantChatSessionORM.status == "active",
            )
            .order_by(
                AssistantChatSessionORM.last_message_at.desc().nullslast(),
                AssistantChatSessionORM.updated_at.desc(),
            )
            .limit(1)
        )
        return self.session.scalars(statement).first()

    def append_message(
        self,
        *,
        chat_session_id: str,
        owner_id: str,
        role: str,
        content: str,
        intent: str | None = None,
        data: JsonDict | None = None,
        payload: JsonDict | None = None,
        created_at: datetime | None = None,
    ) -> AssistantChatMessageORM:
        """追加一条聊天消息，并同步会话计数。"""

        created = created_at or datetime.now().astimezone()
        sequence_no = self.next_sequence_no(chat_session_id=chat_session_id)
        chat_message_id = f"{chat_session_id}:msg:{sequence_no:06d}"
        values = {
            "chat_message_id": chat_message_id,
            "chat_session_id": chat_session_id,
            "owner_id": owner_id,
            "sequence_no": sequence_no,
            "role": role,
            "content": content,
            "intent": intent,
            "data": _json_safe(data or {}),
            "created_at": created,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssistantChatMessageORM).values(**values)
        self.session.execute(
            statement.on_conflict_do_nothing(
                constraint="uq_chat_messages_session_seq",
            )
        )
        count_statement = (
            select(func.count())
            .select_from(AssistantChatMessageORM)
            .where(AssistantChatMessageORM.chat_session_id == chat_session_id)
        )
        message_count = int(self.session.scalar(count_statement) or 0)
        session = self.session.get(AssistantChatSessionORM, chat_session_id)
        if session is not None:
            session.last_message_at = created
            session.message_count = message_count
            session.updated_at = datetime.now().astimezone()
            if not session.title and role == "user":
                session.title = content.strip()[:80]
        self.session.flush()
        return self.session.get_one(AssistantChatMessageORM, chat_message_id)

    def next_sequence_no(self, *, chat_session_id: str) -> int:
        """查询下一条消息序号。"""

        statement = select(func.max(AssistantChatMessageORM.sequence_no)).where(
            AssistantChatMessageORM.chat_session_id == chat_session_id
        )
        return int(self.session.scalar(statement) or 0) + 1

    def list_messages(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        limit: int = 50,
    ) -> list[AssistantChatMessageORM]:
        """按时间顺序查询聊天消息。"""

        statement = (
            select(AssistantChatMessageORM)
            .where(
                AssistantChatMessageORM.owner_id == owner_id,
                AssistantChatMessageORM.chat_session_id == chat_session_id,
            )
            .order_by(AssistantChatMessageORM.sequence_no.asc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_recent_messages(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        limit: int = 20,
    ) -> list[AssistantChatMessageORM]:
        """查询最近消息，并按对话顺序返回。"""

        statement = (
            select(AssistantChatMessageORM)
            .where(
                AssistantChatMessageORM.owner_id == owner_id,
                AssistantChatMessageORM.chat_session_id == chat_session_id,
            )
            .order_by(AssistantChatMessageORM.sequence_no.desc())
            .limit(limit)
        )
        return list(reversed(list(self.session.scalars(statement))))


class DataQualityRepository:
    """数据质量快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_quality_snapshot(
        self,
        *,
        quality_id: str,
        market: str,
        data_domain: str,
        provider: str,
        status: str,
        freshness_status: str,
        checked_at: datetime,
        issue_count: int,
        asset_id: str | None = None,
        symbol: str | None = None,
        latest_data_at: datetime | None = None,
        missing_items: list[str] | None = None,
        payload: JsonDict | None = None,
    ) -> DataQualitySnapshotORM:
        """按 `quality_id` 幂等写入数据质量快照。"""

        values = {
            "quality_id": quality_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "data_domain": data_domain,
            "provider": provider,
            "status": status,
            "freshness_status": freshness_status,
            "latest_data_at": latest_data_at,
            "checked_at": checked_at,
            "missing_items": missing_items or [],
            "issue_count": issue_count,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(DataQualitySnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"quality_id", "checked_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    DataQualitySnapshotORM.quality_id,
                    DataQualitySnapshotORM.checked_at,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            DataQualitySnapshotORM,
            {"quality_id": quality_id, "checked_at": checked_at},
        )

    def list_latest_quality(
        self,
        *,
        asset_id: str | None = None,
        market: str | None = None,
        data_domain: str | None = None,
        limit: int = 20,
    ) -> list[DataQualitySnapshotORM]:
        """查询最近数据质量快照。"""

        statement = select(DataQualitySnapshotORM)
        if asset_id:
            statement = statement.where(DataQualitySnapshotORM.asset_id == asset_id)
        if market:
            statement = statement.where(DataQualitySnapshotORM.market == market)
        if data_domain:
            statement = statement.where(DataQualitySnapshotORM.data_domain == data_domain)
        return list(
            self.session.scalars(
                statement.order_by(DataQualitySnapshotORM.checked_at.desc()).limit(limit)
            )
        )

    def list_recent_quality_since(
        self,
        *,
        asset_ids: Sequence[str],
        since: datetime,
        limit: int = 50,
    ) -> list[DataQualitySnapshotORM]:
        """查询一组资产在时间窗口内新增的数据质量快照。"""

        if not asset_ids:
            return []
        statement = (
            select(DataQualitySnapshotORM)
            .where(
                DataQualitySnapshotORM.asset_id.in_(asset_ids),
                DataQualitySnapshotORM.checked_at >= since,
            )
            .order_by(DataQualitySnapshotORM.checked_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


class WorkflowAuditRepository:
    """上层主 Agent 调用底层金融团队 Workflow 的审计仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_run(
        self,
        *,
        workflow_run_id: str,
        owner_id: str,
        workflow_type: str,
        trigger_type: str,
        status: str,
        started_at: datetime,
        trigger_ref: str | None = None,
        finished_at: datetime | None = None,
        input_ref: str | None = None,
        output_ref: str | None = None,
        payload: JsonDict | None = None,
    ) -> AgentWorkflowRunORM:
        """按 `workflow_run_id` 幂等写入 Workflow 运行审计。"""

        values = {
            "workflow_run_id": workflow_run_id,
            "owner_id": owner_id,
            "workflow_type": workflow_type,
            "trigger_type": trigger_type,
            "trigger_ref": trigger_ref,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "input_ref": input_ref,
            "output_ref": output_ref,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AgentWorkflowRunORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "workflow_run_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AgentWorkflowRunORM.workflow_run_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AgentWorkflowRunORM, workflow_run_id)

    def insert_event(
        self,
        *,
        workflow_event_id: str,
        workflow_run_id: str,
        event_type: str,
        message: str,
        agent_name: str | None = None,
        evidence_ids: list[str] | None = None,
        created_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> AgentWorkflowEventORM:
        """写入或覆盖一条 Workflow 可审计事件。"""

        values = {
            "workflow_event_id": workflow_event_id,
            "workflow_run_id": workflow_run_id,
            "event_type": event_type,
            "agent_name": agent_name,
            "message": message,
            "evidence_ids": evidence_ids or [],
            "created_at": created_at or datetime.now().astimezone(),
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AgentWorkflowEventORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key != "workflow_event_id"
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AgentWorkflowEventORM.workflow_event_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AgentWorkflowEventORM, workflow_event_id)

    def list_events(self, workflow_run_id: str) -> list[AgentWorkflowEventORM]:
        """查询一次 Workflow 的事件。"""

        statement = (
            select(AgentWorkflowEventORM)
            .where(AgentWorkflowEventORM.workflow_run_id == workflow_run_id)
            .order_by(AgentWorkflowEventORM.created_at)
        )
        return list(self.session.scalars(statement))


class CapitalFlowRepository:
    """A 股资金流快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_capital_flow_snapshot(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        window: str,
        source: str,
        status: str,
        as_of: datetime,
        main_net_inflow: Decimal | None = None,
        northbound_net_inflow: Decimal | None = None,
        turnover_rate: Decimal | None = None,
        amount: Decimal | None = None,
        payload: JsonDict | None = None,
    ) -> CapitalFlowSnapshotORM:
        """按 `snapshot_id` 幂等写入资金流快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "main_net_inflow": main_net_inflow,
            "northbound_net_inflow": northbound_net_inflow,
            "turnover_rate": turnover_rate,
            "amount": amount,
            "window": window,
            "source": source,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(CapitalFlowSnapshotORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "snapshot_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[CapitalFlowSnapshotORM.snapshot_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(CapitalFlowSnapshotORM, snapshot_id)

    def upsert_capital_flow_snapshots(
        self,
        snapshots: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入资金流快照。"""

        rows = _dedupe_rows(
            [
                {
                    "snapshot_id": item["snapshot_id"],
                    "asset_id": item["asset_id"],
                    "symbol": item["symbol"],
                    "market": item["market"],
                    "main_net_inflow": item.get("main_net_inflow"),
                    "northbound_net_inflow": item.get("northbound_net_inflow"),
                    "turnover_rate": item.get("turnover_rate"),
                    "amount": item.get("amount"),
                    "window": item["window"],
                    "source": item["source"],
                    "status": item["status"],
                    "as_of": item["as_of"],
                    "payload": _json_safe(item.get("payload") or {}),
                }
                for item in snapshots
            ],
            ("snapshot_id",),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(CapitalFlowSnapshotORM).values(list(chunk))
            update_values = {key: statement.excluded[key] for key in rows[0] if key != "snapshot_id"}
            return statement.on_conflict_do_update(
                index_elements=[CapitalFlowSnapshotORM.snapshot_id],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def get_latest_snapshot(
        self,
        *,
        asset_id: str,
        window: str | None = None,
    ) -> CapitalFlowSnapshotORM | None:
        """查询单标的最新资金流快照。"""

        statement = select(CapitalFlowSnapshotORM).where(
            CapitalFlowSnapshotORM.asset_id == asset_id
        )
        if window:
            statement = statement.where(CapitalFlowSnapshotORM.window == window)
        return self.session.scalars(
            statement.order_by(CapitalFlowSnapshotORM.as_of.desc()).limit(1)
        ).one_or_none()

    def list_recent_snapshots(
        self,
        *,
        asset_id: str,
        limit: int,
        window: str | None = None,
        source: str | None = None,
    ) -> list[CapitalFlowSnapshotORM]:
        """查询单标的最近 N 条资金流快照，返回时间升序结果。"""

        statement = select(CapitalFlowSnapshotORM).where(
            CapitalFlowSnapshotORM.asset_id == asset_id
        )
        if window:
            statement = statement.where(CapitalFlowSnapshotORM.window == window)
        if source:
            statement = statement.where(CapitalFlowSnapshotORM.source == source)
        rows = list(
            self.session.scalars(
                statement.order_by(CapitalFlowSnapshotORM.as_of.desc()).limit(limit)
            )
        )
        return list(reversed(rows))


class EventRepository:
    """事件和证据仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_event(
        self,
        *,
        event_id: str,
        market: str,
        event_type: str,
        title: str,
        sentiment: str,
        importance: str,
        source: str,
        collected_at: datetime,
        asset_id: str | None = None,
        symbol: str | None = None,
        summary: str | None = None,
        url: str | None = None,
        published_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> EventRecordORM:
        """按 `event_id` 幂等写入事件记录。"""

        values = {
            "event_id": event_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "event_type": event_type,
            "title": title,
            "summary": summary,
            "sentiment": sentiment,
            "importance": importance,
            "source": source,
            "url": url,
            "published_at": published_at,
            "collected_at": collected_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(EventRecordORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "event_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[EventRecordORM.event_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(EventRecordORM, event_id)

    def upsert_evidence(
        self,
        *,
        evidence_id: str,
        evidence_type: str,
        source: str,
        title: str,
        reliability: str,
        collected_at: datetime,
        asset_id: str | None = None,
        summary: str | None = None,
        data_ref: str | None = None,
        url: str | None = None,
        as_of: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> EvidenceORM:
        """按 `evidence_id` 幂等写入证据索引。"""

        values = {
            "evidence_id": evidence_id,
            "evidence_type": evidence_type,
            "asset_id": asset_id,
            "source": source,
            "title": title,
            "summary": summary,
            "data_ref": data_ref,
            "url": url,
            "reliability": reliability,
            "as_of": as_of,
            "collected_at": collected_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(EvidenceORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "evidence_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[EvidenceORM.evidence_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(EvidenceORM, evidence_id)

    def upsert_events(
        self,
        events: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入事件记录。"""

        rows = _dedupe_rows(
            [
                {
                    "event_id": item["event_id"],
                    "asset_id": item.get("asset_id"),
                    "symbol": item.get("symbol"),
                    "market": item["market"],
                    "event_type": item["event_type"],
                    "title": item["title"],
                    "summary": item.get("summary"),
                    "sentiment": item.get("sentiment", "unknown"),
                    "importance": item.get("importance", "medium"),
                    "source": item["source"],
                    "url": item.get("url"),
                    "published_at": item.get("published_at"),
                    "collected_at": item["collected_at"],
                    "payload": _json_safe(item.get("payload") or {}),
                }
                for item in events
            ],
            ("event_id",),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(EventRecordORM).values(list(chunk))
            update_values = {key: statement.excluded[key] for key in rows[0] if key != "event_id"}
            return statement.on_conflict_do_update(
                index_elements=[EventRecordORM.event_id],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def upsert_evidence_items(
        self,
        evidence: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入证据索引。"""

        rows = _dedupe_rows(
            [
                {
                    "evidence_id": item["evidence_id"],
                    "evidence_type": item["evidence_type"],
                    "asset_id": item.get("asset_id"),
                    "source": item["source"],
                    "title": item["title"],
                    "summary": item.get("summary"),
                    "data_ref": item.get("data_ref"),
                    "url": item.get("url"),
                    "reliability": item["reliability"],
                    "as_of": item.get("as_of"),
                    "collected_at": item["collected_at"],
                    "payload": _json_safe(item.get("payload") or {}),
                }
                for item in evidence
            ],
            ("evidence_id",),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(EvidenceORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key] for key in rows[0] if key != "evidence_id"
            }
            return statement.on_conflict_do_update(
                index_elements=[EvidenceORM.evidence_id],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def update_event_article_payload(
        self,
        *,
        event_id: str,
        article_payload: JsonDict,
    ) -> EventRecordORM:
        """回填新闻事件的正文抓取 payload。"""

        self.update_event_article_payloads(
            [{"event_id": event_id, "article_payload": article_payload}]
        )
        return self.session.get_one(EventRecordORM, event_id)

    def update_event_article_payloads(
        self,
        updates: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量回填新闻事件的正文抓取 payload。"""

        rows = _article_payload_update_rows(updates)
        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=lambda chunk: _article_payload_update_statement(
                table_name=EventRecordORM.__tablename__,
                match_column="event_id",
                rows=chunk,
            ),
        )

    def update_evidence_article_payloads_by_event(
        self,
        *,
        event_id: str,
        article_payload: JsonDict,
    ) -> int:
        """按事件 ID 回填关联证据的正文抓取 payload。"""

        return self.update_evidence_article_payloads_by_events(
            [{"event_id": event_id, "article_payload": article_payload}]
        )

    def update_evidence_article_payloads_by_events(
        self,
        updates: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """按事件 ID 批量回填关联证据的正文抓取 payload。"""

        rows = _article_payload_update_rows(updates)
        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=lambda chunk: _article_payload_update_statement(
                table_name=EvidenceORM.__tablename__,
                match_column="data_ref",
                rows=chunk,
            ),
        )

    def list_stock_news_for_entity_revalidation(
        self,
        *,
        limit: int | None = None,
    ) -> list[tuple[EventRecordORM, str | None]]:
        """读取全部关键词新闻审计行，并关联资产主表公司名。"""

        statement = (
            select(EventRecordORM, AssetORM.name)
            .outerjoin(AssetORM, AssetORM.asset_id == EventRecordORM.asset_id)
            .where(EventRecordORM.source == STOCK_NEWS_SOURCE)
            .order_by(EventRecordORM.collected_at, EventRecordORM.event_id)
        )
        if limit is not None:
            statement = statement.limit(max(limit, 1))
        return [
            (row[0], row[1])
            for row in self.session.execute(statement).all()
        ]

    def update_news_entity_validations(
        self,
        updates: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量合并历史新闻事件的实体校验 payload。"""

        return _execute_entity_validation_updates(
            self.session,
            table_name=EventRecordORM.__tablename__,
            match_column="event_id",
            updates=updates,
            chunk_size=chunk_size,
        )

    def update_evidence_entity_validations(
        self,
        updates: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """按事件 ID 批量合并关联证据的实体校验 payload。"""

        return _execute_entity_validation_updates(
            self.session,
            table_name=EvidenceORM.__tablename__,
            match_column="data_ref",
            updates=updates,
            chunk_size=chunk_size,
        )

    def list_recent_events(
        self,
        *,
        asset_id: str,
        limit: int = 20,
        max_age_days: int | None = DEFAULT_EVENT_SIGNAL_LOOKBACK_DAYS,
        now: datetime | None = None,
    ) -> list[EventRecordORM]:
        """查询单标的最近事件。

        默认只返回 90 天内事件作为当前信号；审计场景可传入
        `max_age_days=None` 关闭时间窗口。
        """

        statement = (
            select(EventRecordORM)
            .where(
                EventRecordORM.asset_id == asset_id,
                active_event_predicate(EventRecordORM),
            )
            .order_by(
                EventRecordORM.published_at.desc().nullslast(),
                EventRecordORM.collected_at.desc(),
            )
            .limit(limit)
        )
        cutoff = event_signal_cutoff(max_age_days, now=now)
        if cutoff is not None:
            statement = statement.where(
                or_(
                    EventRecordORM.published_at >= cutoff,
                    (
                        EventRecordORM.published_at.is_(None)
                        & (EventRecordORM.collected_at >= cutoff)
                    ),
                )
            )
        return list(self.session.scalars(statement))

    def delete_expired_article_events(
        self,
        *,
        cutoff: datetime,
        event_types: Sequence[str] = NEWS_ARTICLE_EVENT_TYPES,
    ) -> JsonDict:
        """删除过期新闻/公告事件和证据整行，保留 raw_records 原始审计。"""

        event_statement = _expired_article_delete_statement(
            table_name=EventRecordORM.__tablename__,
            type_column="event_type",
            time_column="published_at",
            event_types=event_types,
            cutoff=cutoff,
        )
        evidence_statement = _expired_article_delete_statement(
            table_name=EvidenceORM.__tablename__,
            type_column="evidence_type",
            time_column="as_of",
            event_types=event_types,
            cutoff=cutoff,
        )
        event_result = self.session.execute(event_statement)
        evidence_result = self.session.execute(evidence_statement)
        self.session.flush()
        event_count = int(event_result.rowcount or 0)
        evidence_count = int(evidence_result.rowcount or 0)
        return {
            "event_records": event_count,
            "evidence": evidence_count,
            "total": event_count + evidence_count,
        }


class RiskRepository:
    """风险发现仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_risk_finding(
        self,
        *,
        risk_id: str,
        scope: str,
        risk_type: str,
        severity: str,
        title: str,
        as_of: datetime,
        asset_id: str | None = None,
        score: Decimal | None = None,
        description: str | None = None,
        evidence_ids: list[str] | None = None,
        payload: JsonDict | None = None,
    ) -> RiskFindingORM:
        """按 `risk_id` 幂等写入风险发现。"""

        values = {
            "risk_id": risk_id,
            "asset_id": asset_id,
            "scope": scope,
            "risk_type": risk_type,
            "severity": severity,
            "score": score,
            "title": title,
            "description": description,
            "as_of": as_of,
            "evidence_ids": evidence_ids or [],
            "payload": _json_safe(payload or {}),
        }
        statement = insert(RiskFindingORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "risk_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[RiskFindingORM.risk_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(RiskFindingORM, risk_id)

    def upsert_risk_findings(
        self,
        risks: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入风险发现。"""

        rows = _dedupe_rows(
            [
                {
                    "risk_id": item["risk_id"],
                    "asset_id": item.get("asset_id"),
                    "scope": item["scope"],
                    "risk_type": item["risk_type"],
                    "severity": item["severity"],
                    "score": item.get("score"),
                    "title": item["title"],
                    "description": item.get("description"),
                    "as_of": item["as_of"],
                    "evidence_ids": item.get("evidence_ids") or [],
                    "payload": _json_safe(item.get("payload") or {}),
                }
                for item in risks
            ],
            ("risk_id",),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(RiskFindingORM).values(list(chunk))
            update_values = {key: statement.excluded[key] for key in rows[0] if key != "risk_id"}
            return statement.on_conflict_do_update(
                index_elements=[RiskFindingORM.risk_id],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def list_recent_risks(self, *, asset_id: str, limit: int = 20) -> list[RiskFindingORM]:
        """查询单标的最近风险发现。"""

        statement = (
            select(RiskFindingORM)
            .where(RiskFindingORM.asset_id == asset_id)
            .order_by(RiskFindingORM.as_of.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_recent_risks_since(
        self,
        *,
        asset_ids: Sequence[str],
        since: datetime,
        severities: Sequence[str] = ("high", "critical"),
        limit: int = 50,
    ) -> list[RiskFindingORM]:
        """查询一组资产在时间窗口内新增的高优先级风险。"""

        if not asset_ids:
            return []
        statement = (
            select(RiskFindingORM)
            .where(
                RiskFindingORM.asset_id.in_(asset_ids),
                RiskFindingORM.as_of >= since,
                RiskFindingORM.severity.in_(severities),
            )
            .order_by(RiskFindingORM.as_of.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


class DerivativeDataRepository:
    """数字货币衍生品快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_crypto_derivative_snapshot(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        source: str,
        as_of: datetime,
        funding_rate: Decimal | None = None,
        next_funding_time: datetime | None = None,
        open_interest: Decimal | None = None,
        open_interest_value: Decimal | None = None,
        long_short_ratio: Decimal | None = None,
        basis_rate: Decimal | None = None,
        liquidation_risk_score: Decimal | None = None,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> CryptoDerivativeSnapshotORM:
        """按 `asset_id + as_of + source` 幂等写入衍生品快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "source": source,
            "as_of": as_of,
            "funding_rate": funding_rate,
            "next_funding_time": next_funding_time,
            "open_interest": open_interest,
            "open_interest_value": open_interest_value,
            "long_short_ratio": long_short_ratio,
            "basis_rate": basis_rate,
            "liquidation_risk_score": liquidation_risk_score,
            "status": status,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(CryptoDerivativeSnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"asset_id", "as_of", "source"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    CryptoDerivativeSnapshotORM.asset_id,
                    CryptoDerivativeSnapshotORM.as_of,
                    CryptoDerivativeSnapshotORM.source,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_snapshot(asset_id=asset_id, as_of=as_of, source=source)

    def upsert_crypto_derivative_snapshots(
        self,
        snapshots: Sequence[JsonDict],
        *,
        chunk_size: int = 500,
    ) -> int:
        """批量写入数字货币衍生品快照。"""

        rows = _dedupe_rows(
            [
                {
                    "snapshot_id": item["snapshot_id"],
                    "asset_id": item["asset_id"],
                    "symbol": item["symbol"],
                    "market": item["market"],
                    "source": item["source"],
                    "as_of": item["as_of"],
                    "funding_rate": item.get("funding_rate"),
                    "next_funding_time": item.get("next_funding_time"),
                    "open_interest": item.get("open_interest"),
                    "open_interest_value": item.get("open_interest_value"),
                    "long_short_ratio": item.get("long_short_ratio"),
                    "basis_rate": item.get("basis_rate"),
                    "liquidation_risk_score": item.get("liquidation_risk_score"),
                    "status": item.get("status", "available"),
                    "payload": _json_safe(item.get("payload") or {}),
                }
                for item in snapshots
            ],
            ("asset_id", "as_of", "source"),
        )

        def build_statement(chunk: Sequence[JsonDict]) -> Any:
            statement = insert(CryptoDerivativeSnapshotORM).values(list(chunk))
            update_values = {
                key: statement.excluded[key]
                for key in rows[0]
                if key not in {"asset_id", "as_of", "source"}
            }
            return statement.on_conflict_do_update(
                index_elements=[
                    CryptoDerivativeSnapshotORM.asset_id,
                    CryptoDerivativeSnapshotORM.as_of,
                    CryptoDerivativeSnapshotORM.source,
                ],
                set_=update_values,
            )

        return _execute_chunked_upserts(
            self.session,
            rows,
            chunk_size=chunk_size,
            build_statement=build_statement,
        )

    def get_snapshot(
        self,
        *,
        asset_id: str,
        as_of: datetime,
        source: str,
    ) -> CryptoDerivativeSnapshotORM:
        """根据复合键查询单条衍生品快照。"""

        return self.session.get_one(
            CryptoDerivativeSnapshotORM,
            {
                "asset_id": asset_id,
                "as_of": as_of,
                "source": source,
            },
        )

    def get_latest_snapshot(
        self,
        *,
        asset_id: str,
        source: str | None = None,
    ) -> CryptoDerivativeSnapshotORM | None:
        """查询单标的最新衍生品快照。"""

        statement = select(CryptoDerivativeSnapshotORM).where(
            CryptoDerivativeSnapshotORM.asset_id == asset_id
        )
        if source:
            statement = statement.where(CryptoDerivativeSnapshotORM.source == source)
        return self.session.scalars(
            statement.order_by(CryptoDerivativeSnapshotORM.as_of.desc()).limit(1)
        ).one_or_none()

    def list_recent_snapshots(
        self,
        *,
        asset_id: str,
        limit: int,
        source: str | None = None,
    ) -> list[CryptoDerivativeSnapshotORM]:
        """查询单标的最近 N 条衍生品快照，返回时间升序结果。"""

        statement = select(CryptoDerivativeSnapshotORM).where(
            CryptoDerivativeSnapshotORM.asset_id == asset_id
        )
        if source:
            statement = statement.where(CryptoDerivativeSnapshotORM.source == source)
        rows = list(
            self.session.scalars(
                statement.order_by(CryptoDerivativeSnapshotORM.as_of.desc()).limit(limit)
            )
        )
        return list(reversed(rows))


class ModelRuntimeConfigRepository:
    """模型供应商、模型实例、路由规则和检索配置仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_provider(
        self,
        *,
        provider_key: str,
        provider_vendor: str,
        provider_name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 30,
        is_enabled: bool = True,
        is_default: bool = False,
        payload: JsonDict | None = None,
    ) -> ModelProviderORM:
        """按 provider_key 幂等写入模型供应商配置。"""

        values = {
            "provider_id": f"model_provider:{provider_key}",
            "provider_key": provider_key,
            "provider_vendor": provider_vendor,
            "provider_name": provider_name,
            "base_url": base_url,
            "api_key": api_key,
            "timeout_seconds": timeout_seconds,
            "is_enabled": is_enabled,
            "is_default": is_default,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(ModelProviderORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"provider_id", "provider_key"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_model_providers_provider_key",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_provider(provider_key)

    def upsert_model_instance(
        self,
        *,
        provider_key: str,
        model_key: str,
        model_name: str,
        model_type: str = "llm",
        role: str | None = None,
        route_priority: int = 0,
        timeout_seconds: int = 30,
        is_enabled: bool = True,
        is_default: bool = False,
        payload: JsonDict | None = None,
    ) -> ModelInstanceORM:
        """按 model_key 幂等写入模型实例配置。"""

        values = {
            "model_instance_id": f"model_instance:{model_key}",
            "provider_key": provider_key,
            "model_key": model_key,
            "model_type": model_type,
            "model_name": model_name,
            "role": role,
            "route_priority": route_priority,
            "timeout_seconds": timeout_seconds,
            "is_enabled": is_enabled,
            "is_default": is_default,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(ModelInstanceORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"model_instance_id", "model_key"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_model_instances_model_key",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_model_instance(model_key)

    def upsert_routing_rule(
        self,
        *,
        workflow_type: str,
        task: str,
        role: str,
        model_key: str,
        decision_type: str = "",
        reason: str | None = None,
        priority: int = 0,
        is_enabled: bool = True,
        payload: JsonDict | None = None,
    ) -> ModelRoutingRuleORM:
        """写入模型路由规则，用于覆盖默认路由策略。"""

        decision_type = decision_type or ""
        values = {
            "rule_id": build_model_route_rule_id(
                workflow_type=workflow_type,
                task=task,
                role=role,
                decision_type=decision_type,
            ),
            "workflow_type": workflow_type,
            "task": task,
            "role": role,
            "model_key": model_key,
            "decision_type": decision_type,
            "reason": reason,
            "priority": priority,
            "is_enabled": is_enabled,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(ModelRoutingRuleORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"rule_id", "workflow_type", "task", "role", "decision_type"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_model_routing_rules_scope",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_routing_rule(
            workflow_type=workflow_type,
            task=task,
            role=role,
            decision_type=decision_type,
        )

    def upsert_retrieval_profile(
        self,
        *,
        profile_key: str,
        profile_name: str,
        usage_scope: str,
        search_method: str,
        embedding_model_key: str | None = None,
        rerank_model_key: str | None = None,
        top_k: int = 4,
        score_threshold: Decimal | None = None,
        reranking_enable: bool = False,
        reranking_mode: str | None = None,
        weights: JsonDict | None = None,
        is_enabled: bool = True,
        is_default: bool = False,
        payload: JsonDict | None = None,
    ) -> RetrievalProfileORM:
        """写入检索配置，供后续向量/RAG 召回链路复用。"""

        values = {
            "profile_id": f"retrieval_profile:{profile_key}",
            "profile_key": profile_key,
            "profile_name": profile_name,
            "usage_scope": usage_scope,
            "search_method": search_method,
            "embedding_model_key": embedding_model_key,
            "rerank_model_key": rerank_model_key,
            "top_k": top_k,
            "score_threshold": score_threshold,
            "reranking_enable": reranking_enable,
            "reranking_mode": reranking_mode,
            "weights": _json_safe(weights or {}),
            "is_enabled": is_enabled,
            "is_default": is_default,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(RetrievalProfileORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"profile_id", "profile_key"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_retrieval_profiles_profile_key",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_retrieval_profile(profile_key)

    def get_provider(self, provider_key: str) -> ModelProviderORM:
        """按 provider_key 查询供应商。"""

        statement = select(ModelProviderORM).where(ModelProviderORM.provider_key == provider_key)
        return self.session.scalars(statement).one()

    def get_model_instance(self, model_key: str) -> ModelInstanceORM:
        """按 model_key 查询模型实例。"""

        statement = select(ModelInstanceORM).where(ModelInstanceORM.model_key == model_key)
        return self.session.scalars(statement).one()

    def disable_model_instance(self, model_key: str) -> ModelInstanceORM:
        """软删除模型实例，并停用所有指向它的路由规则。"""

        model = self.get_model_instance(model_key)
        model.is_enabled = False
        model.updated_at = datetime.now().astimezone()
        statement = select(ModelRoutingRuleORM).where(ModelRoutingRuleORM.model_key == model_key)
        for route in self.session.scalars(statement):
            route.is_enabled = False
            route.updated_at = model.updated_at
        self.session.flush()
        return model

    def get_routing_rule(
        self,
        *,
        workflow_type: str,
        task: str,
        role: str,
        decision_type: str = "",
    ) -> ModelRoutingRuleORM:
        """查询一条精确路由规则。"""

        statement = select(ModelRoutingRuleORM).where(
            ModelRoutingRuleORM.workflow_type == workflow_type,
            ModelRoutingRuleORM.task == task,
            ModelRoutingRuleORM.role == role,
            ModelRoutingRuleORM.decision_type == (decision_type or ""),
        )
        return self.session.scalars(statement).one()

    def get_retrieval_profile(self, profile_key: str) -> RetrievalProfileORM:
        """按 profile_key 查询检索配置。"""

        statement = select(RetrievalProfileORM).where(
            RetrievalProfileORM.profile_key == profile_key
        )
        return self.session.scalars(statement).one()

    def list_providers(self, *, enabled_only: bool = False) -> list[ModelProviderORM]:
        """列出模型供应商。"""

        statement = select(ModelProviderORM)
        if enabled_only:
            statement = statement.where(ModelProviderORM.is_enabled.is_(True))
        return list(self.session.scalars(statement.order_by(ModelProviderORM.provider_key)))

    def list_model_instances(self, *, enabled_only: bool = False) -> list[ModelInstanceORM]:
        """列出模型实例。"""

        statement = select(ModelInstanceORM)
        if enabled_only:
            statement = statement.where(ModelInstanceORM.is_enabled.is_(True))
        return list(
            self.session.scalars(
                statement.order_by(
                    ModelInstanceORM.route_priority.desc(),
                    ModelInstanceORM.model_key,
                )
            )
        )

    def list_routing_rules(self, *, enabled_only: bool = False) -> list[ModelRoutingRuleORM]:
        """列出模型路由规则。"""

        statement = select(ModelRoutingRuleORM)
        if enabled_only:
            statement = statement.where(ModelRoutingRuleORM.is_enabled.is_(True))
        return list(
            self.session.scalars(
                statement.order_by(
                    ModelRoutingRuleORM.workflow_type,
                    ModelRoutingRuleORM.task,
                    ModelRoutingRuleORM.role,
                    ModelRoutingRuleORM.priority.desc(),
                )
            )
        )

    def list_retrieval_profiles(
        self,
        *,
        enabled_only: bool = False,
    ) -> list[RetrievalProfileORM]:
        """列出检索配置。"""

        statement = select(RetrievalProfileORM)
        if enabled_only:
            statement = statement.where(RetrievalProfileORM.is_enabled.is_(True))
        return list(
            self.session.scalars(
                statement.order_by(RetrievalProfileORM.usage_scope, RetrievalProfileORM.profile_key)
            )
        )

    def get_enabled_provider_map(self) -> dict[str, ModelProviderORM]:
        """返回启用供应商的 provider_key 映射。"""

        return {
            provider.provider_key: provider for provider in self.list_providers(enabled_only=True)
        }

    def find_route_model(
        self,
        *,
        workflow_type: str,
        task: str,
        role: str,
        decision_type: str | None = None,
    ) -> tuple[ModelRoutingRuleORM, ModelInstanceORM] | None:
        """按精确到宽松的顺序查找可用路由和模型实例。"""

        decision_candidates = [decision_type or "", ""]
        scopes = [
            (workflow_type, task),
            (workflow_type, "*"),
            ("*", task),
            ("*", "*"),
        ]
        for candidate_decision_type in decision_candidates:
            for candidate_workflow_type, candidate_task in scopes:
                statement = (
                    select(ModelRoutingRuleORM)
                    .where(
                        ModelRoutingRuleORM.workflow_type == candidate_workflow_type,
                        ModelRoutingRuleORM.task == candidate_task,
                        ModelRoutingRuleORM.role == role,
                        ModelRoutingRuleORM.decision_type == candidate_decision_type,
                        ModelRoutingRuleORM.is_enabled.is_(True),
                    )
                    .order_by(ModelRoutingRuleORM.priority.desc())
                    .limit(1)
                )
                rule = self.session.scalars(statement).one_or_none()
                if rule is None:
                    continue
                model = self.find_enabled_model(rule.model_key)
                if model is not None:
                    return rule, model
        return None

    def find_enabled_model(self, model_key: str) -> ModelInstanceORM | None:
        """查找启用中的模型实例。"""

        statement = (
            select(ModelInstanceORM)
            .where(
                ModelInstanceORM.model_key == model_key,
                ModelInstanceORM.is_enabled.is_(True),
            )
            .limit(1)
        )
        return self.session.scalars(statement).one_or_none()

    def count_model_instances(self) -> int:
        """统计模型实例数量。"""

        statement = select(func.count()).select_from(ModelInstanceORM)
        return int(self.session.scalar(statement) or 0)

    def seed_default_model_runtime_config(
        self,
        *,
        overwrite: bool = False,
        deepseek_base_url: str | None = None,
        deepseek_api_key: str | None = None,
        openai_base_url: str | None = None,
        openai_api_key: str | None = None,
        embedding_model_key: str | None = None,
        rerank_model_key: str | None = None,
    ) -> JsonDict:
        """写入默认双模型和默认检索配置。"""

        if not overwrite and self.count_model_instances() > 0:
            return {
                "seeded": False,
                "reason": "数据库中已经存在模型实例，未覆盖现有配置。",
                "model_count": self.count_model_instances(),
            }

        self.upsert_provider(
            provider_key="deepseek",
            provider_vendor="deepseek",
            provider_name="DeepSeek",
            base_url=deepseek_base_url,
            api_key=deepseek_api_key,
            is_default=True,
        )
        self.upsert_provider(
            provider_key="openai",
            provider_vendor="openai",
            provider_name="OpenAI",
            base_url=openai_base_url,
            api_key=openai_api_key,
            is_default=False,
        )
        self.upsert_model_instance(
            provider_key="deepseek",
            model_key="deepseek-v4-pro",
            model_name="DeepSeek V4 Pro",
            role="primary_financial_analyst",
            is_default=True,
            route_priority=100,
        )
        self.upsert_model_instance(
            provider_key="openai",
            model_key="gpt-5.5-pro",
            model_name="GPT-5.5 Pro",
            role="high_risk_reviewer",
            is_default=False,
            route_priority=90,
        )
        self.upsert_routing_rule(
            workflow_type="*",
            task="*",
            role="primary_financial_analyst",
            model_key="deepseek-v4-pro",
            reason="常规金融分析默认使用 DeepSeek V4 Pro。",
            priority=100,
        )
        self.upsert_routing_rule(
            workflow_type="*",
            task="high_risk_review",
            role="high_risk_reviewer",
            model_key="gpt-5.5-pro",
            reason="高风险复核默认使用 GPT-5.5 Pro。",
            priority=100,
        )
        self.upsert_retrieval_profile(
            profile_key="finance_memory_default",
            profile_name="Finance Memory 默认检索配置",
            usage_scope="finance_memory",
            search_method="hybrid_search",
            embedding_model_key=embedding_model_key,
            rerank_model_key=rerank_model_key,
            top_k=8,
            reranking_enable=bool(rerank_model_key),
            reranking_mode="reranking_model" if rerank_model_key else "weighted_score",
            weights={
                "semantic": 0.65,
                "keyword": 0.35,
            },
            is_default=True,
        )
        return {
            "seeded": True,
            "providers": ["deepseek", "openai"],
            "models": ["deepseek-v4-pro", "gpt-5.5-pro"],
            "routing_rules": ["primary_financial_analyst", "high_risk_reviewer"],
            "retrieval_profiles": ["finance_memory_default"],
        }


def build_model_route_rule_id(
    *,
    workflow_type: str,
    task: str,
    role: str,
    decision_type: str,
) -> str:
    """生成模型路由规则 ID。"""

    clean = ":".join(
        part.replace(":", "_") for part in (workflow_type, task, role, decision_type or "any")
    )
    return f"model_route:{clean}"
