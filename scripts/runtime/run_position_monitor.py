"""运行盘中持仓监控进程。

该进程只消费数据库中的活跃持仓和最新行情快照，不直接访问 GoTDX。
实时行情由独立的行情监控进程负责写入数据库，本进程每轮重新评估持仓事实
和行情时效，业务事件由仓储幂等保存。
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from finance_agent.monitoring.service import PositionMonitoringService  # noqa: E402
from finance_agent.storage.db import create_session_factory, session_scope  # noqa: E402
from finance_agent.storage.repositories import AssetRepository, PortfolioRepository  # noqa: E402
from finance_agent.triggers.service import TriggerService  # noqa: E402

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """构建持仓监控命令行参数。"""

    parser = argparse.ArgumentParser(description="运行盘中持仓监控。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="常驻运行直到收到停止信号。")
    mode.add_argument("--once", action="store_true", help="只执行一次监控。")
    parser.add_argument("--owner-id", default="default-owner", help="需要监控的用户 ID。")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="轮询间隔秒数。")
    parser.add_argument(
        "--status-file",
        default="runtime/position_monitor/status.json",
        help="健康状态 JSON 文件路径。",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    return parser


class PositionMonitorRunner:
    """按轮询周期重新评估持仓和行情，仅生成监控建议。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        owner_id: str = "default-owner",
        service_factory: Callable[[Any], PositionMonitoringService] = PositionMonitoringService,
        trigger_factory: Callable[[Any], Any] | None = TriggerService,
        now_factory: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        max_backoff_seconds: float = 60.0,
    ) -> None:
        self.session_factory = session_factory
        self.owner_id = owner_id
        self.service_factory = service_factory
        self.trigger_factory = trigger_factory
        self.now_factory = now_factory
        self.max_backoff_seconds = max(1.0, float(max_backoff_seconds))
        self._failure_count = 0

    def run_once(self, *, status_file: Path | None = None) -> dict[str, Any]:
        """执行一轮监控；行情未变时仍需检查持仓变更和行情过期。"""

        evaluated_at = self.now_factory()
        result: dict[str, Any] = {
            "owner_id": self.owner_id,
            "evaluated_at": evaluated_at.isoformat(),
        }
        try:
            with session_scope(self.session_factory) as session:
                token = self._latest_snapshot_token(session)
                summary = self.service_factory(session).evaluate_owner(
                    self.owner_id,
                    as_of=evaluated_at,
                )
                if self.trigger_factory is not None and summary.changed_actions:
                    # 持久化状态已确认新变化，动作冷却会吞掉 A->B->A 的再次变化。
                    self.trigger_factory(session).persist_position_actions(
                        summary.changed_actions,
                        as_of=evaluated_at,
                        cooldown_minutes=0,
                    )
            result.update(
                snapshot_token=list(token),
                action_count=len(summary.actions),
                error_count=summary.error_count,
            )
            # 先提交已生成的保护性动作，再将部分失败交给统一退避逻辑重试。
            if summary.error_count:
                raise RuntimeError(f"{summary.error_count} 个持仓监控失败")
            self._failure_count = 0
            result["status"] = "completed"
        except Exception as exc:  # noqa: BLE001 - 常驻进程需隔离单轮故障
            self._failure_count += 1
            backoff = min(
                self.max_backoff_seconds,
                max(1.0, 2.0 ** min(self._failure_count - 1, 6)),
            )
            result.update(status="error", error=str(exc)[:500], backoff_seconds=backoff)
            logger.exception("持仓监控执行失败，将在 %.1f 秒后重试", backoff)
        if status_file is not None:
            _write_status(status_file, result)
        return result

    def _latest_snapshot_token(self, session: Any) -> tuple[str, ...]:
        """读取活跃持仓对应行情的快照 ID，仅用于健康状态诊断。"""

        positions = PortfolioRepository(session).list_active_positions_by_owner(
            owner_id=self.owner_id,
            market="ashare",
        )
        asset_ids = [str(getattr(position, "asset_id", "")) for position in positions]
        if not asset_ids:
            return ()
        rows = AssetRepository(session).list_intraday_quote_latest(
            asset_ids=asset_ids,
            quality_statuses=("available", "partial", "conflict"),
        )
        values: list[str] = []
        for row in sorted(
            rows,
            key=lambda item: (str(getattr(item, "asset_id", "")), str(getattr(item, "source", ""))),
        ):
            asset_id = str(getattr(row, "asset_id", "") or "")
            source = str(getattr(row, "source", "") or "")
            snapshot_id = str(getattr(row, "data_snapshot_id", "") or "")
            if not snapshot_id:
                snapshot_id = str(getattr(row, "as_of", "") or "")
            values.append(f"{asset_id}:{source}:{snapshot_id}")
        return tuple(values)


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    """原子写入健康状态文件，避免监控页面读取半截 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    """运行一次或持续运行，停止信号只停止下一轮调度。"""

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    runner = PositionMonitorRunner(
        session_factory=create_session_factory(),
        owner_id=args.owner_id,
        trigger_factory=TriggerService,
    )
    stop_event = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    status_file = Path(args.status_file)
    poll_seconds = max(0.2, float(args.poll_seconds))
    while True:
        result = runner.run_once(status_file=status_file)
        logger.info("持仓监控完成 summary=%s", json.dumps(result, ensure_ascii=False))
        if args.once or not args.loop or stop_event.is_set():
            return 0 if result.get("status") != "error" else 1
        wait_seconds = (
            float(result.get("backoff_seconds", poll_seconds))
            if result.get("status") == "error"
            else poll_seconds
        )
        if stop_event.wait(max(0.2, wait_seconds)):
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
