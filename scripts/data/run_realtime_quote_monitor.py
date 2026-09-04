"""运行持仓和重点池的低延迟实时行情监控。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_agent.intraday.models import QuoteChannelName
    from finance_agent.intraday.quote_monitor import QuoteChannelCollection

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """构建实时监控命令行参数。"""

    parser = argparse.ArgumentParser(description="运行分层实时行情监控。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="常驻运行直到收到停止信号。")
    mode.add_argument("--once", action="store_true", help="只执行一次到期通道。")
    parser.add_argument("--owner-id", default="default-owner", help="需要监控的用户 ID。")
    parser.add_argument(
        "--status-file",
        default="runtime/realtime_quote_monitor/status.json",
        help="健康状态 JSON 文件路径。",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    return parser


class SqlRealtimeSymbolSource:
    """从当前持仓、观察池和显式校验池读取通道代码。"""

    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory

    def symbols_for(
        self,
        channel: QuoteChannelName,
        *,
        owner_id: str,
    ) -> tuple[str, ...]:
        from finance_agent.storage.repositories import PortfolioRepository, WatchlistRepository

        if channel == "verification":
            return _environment_symbols("FINANCE_AGENT_REALTIME_VERIFICATION_SYMBOLS")
        with self.session_factory() as session:
            if channel == "held":
                symbols = (
                    position.symbol
                    for portfolio in PortfolioRepository(session).list_portfolios(
                        owner_id=owner_id,
                        status="active",
                    )
                    for position in PortfolioRepository(session).list_positions(
                        portfolio.portfolio_id,
                        status="active",
                    )
                    if position.market == "ashare" and position.quantity > 0
                )
            else:
                symbols = (
                    item.symbol
                    for item in WatchlistRepository(session).list_active_items(
                        owner_id=owner_id
                    )
                    if item.market == "ashare"
                )
            return _gateway_symbols(symbols)


class DatabaseQuoteCollector:
    """在网络采集完成后开启短事务写入全部实时事实。"""

    def __init__(self, session_factory: Any) -> None:
        from finance_agent.data.providers import AkshareProvider, GotdxGatewayProvider

        self.session_factory = session_factory
        self.gotdx = GotdxGatewayProvider()
        self.akshare = AkshareProvider()

    def collect(
        self,
        *,
        channel: QuoteChannelName,
        symbols: tuple[str, ...],
        captured_at: datetime,
    ) -> QuoteChannelCollection:
        from finance_agent.intraday.quote_monitor import (
            QuoteChannelCollection,
            RealtimeQuoteBatchPersister,
        )
        from finance_agent.storage.db import session_scope
        from finance_agent.storage.repositories import (
            AssetRepository,
            DataSnapshotRepository,
            MarketDataRepository,
        )
        from finance_agent.storage.snapshot_contracts import build_data_snapshot
        from scripts.data.collect_base_data import _fetch_akshare_quote_rows

        started = time.perf_counter()
        if channel in {"held", "radar"}:
            batch = self.gotdx.collect_snapshot_rows(list(symbols), now=captured_at)
            snapshot = batch.snapshot
            rows = batch.rows
        else:
            raw_rows = _fetch_akshare_quote_rows(self.akshare, symbols)
            if not raw_rows:
                raise RuntimeError("AKShare 校验通道没有返回请求标的")
            as_of = max(row["as_of"] for row in raw_rows)
            snapshot = build_data_snapshot(
                snapshot_type="ashare_realtime_quotes_verification",
                market="ashare",
                as_of=as_of,
                captured_at=max(captured_at, as_of),
                provider="akshare:stock_zh_a_spot",
                provider_version="runtime-v1",
                quality_status=_aggregate_status(raw_rows),
                payload={"symbols": list(symbols)},
                metadata={"channel": channel, "quote_count": len(raw_rows)},
            )
            rows = tuple(
                dict(
                    row,
                    source="akshare:stock_zh_a_spot",
                    data_snapshot_id=snapshot.data_snapshot_id,
                )
                for row in raw_rows
            )

        with session_scope(self.session_factory) as session:
            result = RealtimeQuoteBatchPersister(
                snapshot_repository=DataSnapshotRepository(session),
                asset_repository=AssetRepository(session),
                market_repository=MarketDataRepository(session),
            ).persist(snapshot=snapshot, rows=rows, close_before=captured_at)
        latency = time.perf_counter() - started
        return QuoteChannelCollection(
            status=snapshot.quality_status,
            requested_count=len(symbols),
            received_count=len(rows),
            rows_written=result.latest_rows_written,
            bars_written=result.bars_written,
            latency_seconds=latency,
            data_snapshot_id=snapshot.data_snapshot_id,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """运行一次或持续运行，停止信号不会打断当前事务。"""

    from finance_agent.intraday.quote_monitor import RealtimeQuoteMonitor
    from finance_agent.storage.db import create_session_factory

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    session_factory = create_session_factory()
    monitor = RealtimeQuoteMonitor(
        symbol_source=SqlRealtimeSymbolSource(session_factory),
        collector=DatabaseQuoteCollector(session_factory),
    )
    stop_event = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while True:
        summary = monitor.run_due_channels(owner_id=args.owner_id)
        if summary.executed_channels:
            _write_status(Path(args.status_file), summary.to_dict())
            logger.info(
                "实时行情通道完成 summary=%s",
                json.dumps(summary.to_dict(), ensure_ascii=False),
            )
        if args.once or not args.loop or stop_event.is_set():
            return 0
        stop_event.wait(0.2)


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _environment_symbols(name: str) -> tuple[str, ...]:
    return _gateway_symbols(os.getenv(name, "").split(","))


def _gateway_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    from finance_agent.data.normalizers import normalize_ashare_symbol

    result: list[str] = []
    for raw in symbols:
        symbol = normalize_ashare_symbol(str(raw or ""))
        if not symbol:
            continue
        if "." not in symbol:
            suffix = "BJ" if symbol[0] in {"4", "8"} else "SH" if symbol[0] == "6" else "SZ"
            symbol = f"{symbol}.{suffix}"
        if symbol not in result:
            result.append(symbol)
    return tuple(result)


def _aggregate_status(rows: Sequence[dict[str, Any]]) -> str:
    statuses = {str(row.get("quality_status") or "available") for row in rows}
    return next(iter(statuses)) if len(statuses) == 1 else "partial"


if __name__ == "__main__":
    raise SystemExit(main())
