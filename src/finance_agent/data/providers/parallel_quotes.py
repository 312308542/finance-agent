"""gotdx 与 AKShare 的并行行情评估。

两个 Provider 同时采集并分别保留来源，不互相替代。AKShare 的时间戳和
新鲜度由调用方提供，评估器只负责统一快照绑定、来源对比和指标计算。
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

JsonDict = dict[str, Any]
QuoteFetcher = Callable[[tuple[str, ...]], list[JsonDict]]
DEFAULT_CONFLICT_RELATIVE_THRESHOLD = Decimal("0.010000")


@dataclass(frozen=True)
class ParallelQuoteResult:
    """双源行情评估结果。"""

    rows: tuple[JsonDict, ...]
    metrics: JsonDict
    errors: JsonDict


class ParallelQuoteEvaluator:
    """并行执行两个行情 Provider，并返回可审计的统一结果。"""

    def __init__(
        self,
        gotdx_fetcher: QuoteFetcher,
        akshare_fetcher: QuoteFetcher,
        *,
        conflict_relative_threshold: Decimal = DEFAULT_CONFLICT_RELATIVE_THRESHOLD,
    ) -> None:
        if conflict_relative_threshold < 0:
            raise ValueError("conflict_relative_threshold 不能为负数")
        self._fetchers = (
            ("gotdx:tdx_main", gotdx_fetcher),
            ("akshare:stock_zh_a_spot", akshare_fetcher),
        )
        self.conflict_relative_threshold = Decimal(conflict_relative_threshold)

    def evaluate(
        self,
        *,
        symbols: tuple[str, ...] | list[str],
        data_snapshot_id: str,
    ) -> ParallelQuoteResult:
        """并行采集并计算跨源价格差，不因单源失败而跳过另一源。"""

        normalized_symbols = tuple(str(symbol).strip() for symbol in symbols if str(symbol).strip())
        if not normalized_symbols:
            raise ValueError("symbols 不能为空")
        snapshot_id = str(data_snapshot_id).strip()
        if not snapshot_id:
            raise ValueError("data_snapshot_id 不能为空")

        def run(item: tuple[str, QuoteFetcher]) -> tuple[str, list[JsonDict] | None, str | None]:
            source, fetcher = item
            try:
                rows = fetcher(normalized_symbols)
            except Exception as exc:  # noqa: BLE001 - Provider 故障作为数据质量记录
                return source, None, str(exc)
            return source, rows, None

        rows: list[JsonDict] = []
        errors: JsonDict = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="quote-provider") as executor:
            results = list(executor.map(run, self._fetchers))
        for source, source_rows, error in results:
            if error is not None:
                errors[source] = error
                continue
            for raw in source_rows or []:
                row = dict(raw)
                row["source"] = source
                row["data_snapshot_id"] = snapshot_id
                rows.append(row)

        conflicts = self._price_conflicts(rows)
        for row in rows:
            conflict = conflicts.get(str(row.get("asset_id") or ""))
            if conflict is None:
                continue
            row["quality_status"] = "conflict"
            row["status"] = "conflict"
            payload = dict(row.get("payload") or {})
            payload["cross_source_quality"] = "conflict"
            payload["cross_source_conflict"] = conflict
            row["payload"] = payload

        metrics = {
            "source_count": len({str(row["source"]) for row in rows}),
            "row_count": len(rows),
            "price_delta": self._price_deltas(rows),
            "conflicts": conflicts,
            "conflict_relative_threshold": self.conflict_relative_threshold,
        }
        return ParallelQuoteResult(rows=tuple(rows), metrics=metrics, errors=errors)

    @staticmethod
    def _price_deltas(rows: list[JsonDict]) -> dict[str, Decimal]:
        by_asset: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            asset_id = str(row.get("asset_id") or "").strip()
            if not asset_id:
                continue
            try:
                price = Decimal(str(row.get("last_price")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            by_asset.setdefault(asset_id, {})[str(row["source"])] = price
        deltas: dict[str, Decimal] = {}
        for asset_id, prices in by_asset.items():
            if len(prices) >= 2:
                values = list(prices.values())
                deltas[asset_id] = abs(values[0] - values[1])
        return deltas

    def _price_conflicts(self, rows: list[JsonDict]) -> dict[str, JsonDict]:
        """按最小正价格计算跨源偏差，避免把慢源的正常小幅漂移误报为冲突。"""

        by_asset: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            asset_id = str(row.get("asset_id") or "").strip()
            if not asset_id:
                continue
            try:
                price = Decimal(str(row.get("last_price")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            by_asset.setdefault(asset_id, {})[str(row["source"])] = price

        conflicts: dict[str, JsonDict] = {}
        for asset_id, prices in by_asset.items():
            if len(prices) < 2:
                continue
            values = list(prices.values())
            low_price = min(values)
            high_price = max(values)
            relative_delta = (high_price - low_price) / low_price
            rounded_delta = relative_delta.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            if relative_delta > self.conflict_relative_threshold:
                conflicts[asset_id] = {
                    "relative_delta": rounded_delta,
                    "low_price": low_price,
                    "high_price": high_price,
                    "sources": dict(prices),
                }
        return conflicts


def clear_intraday_quote_cache(cache: MutableMapping[Any, Any]) -> int:
    """清理收盘后不再需要的盘中临时行情缓存。"""

    count = len(cache)
    cache.clear()
    return count
