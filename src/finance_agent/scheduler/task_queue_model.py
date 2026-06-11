"""数据同步任务队列的通用运行模型。

本模块只定义任务 item、运行快照和 Provider 降级结果，不直接访问数据库、
Redis 或外部数据源。业务采集任务可以逐步迁移到这些结构上。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

JsonDict = dict[str, Any]
TaskItemStatus = Literal["pending", "running", "completed", "failed", "skipped", "cancelled"]
ProviderAttemptStatus = Literal["ok", "error", "skipped"]


@dataclass(frozen=True)
class ProviderSource:
    """一个可尝试的数据源。"""

    name: str
    rate_key: str | None = None
    complexity: str | None = None
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderFetchResult:
    """单个 Provider 请求的标准化返回。"""

    status: ProviderAttemptStatus
    payload: Any = None
    row_count: int = 0
    error_message: str | None = None

    @classmethod
    def ok(cls, *, payload: Any = None, row_count: int = 0) -> "ProviderFetchResult":
        """创建成功结果。"""

        return cls(status="ok", payload=payload, row_count=max(0, int(row_count)))

    @classmethod
    def error(cls, error_message: str, *, payload: Any = None) -> "ProviderFetchResult":
        """创建失败结果。"""

        return cls(status="error", payload=payload, error_message=str(error_message))

    @classmethod
    def skipped(cls, reason: str) -> "ProviderFetchResult":
        """创建跳过结果。"""

        return cls(status="skipped", error_message=str(reason))


@dataclass(frozen=True)
class ProviderAttempt:
    """一次 Provider 尝试记录。"""

    source: str
    status: ProviderAttemptStatus
    rate_key: str | None = None
    complexity: str | None = None
    row_count: int = 0
    error_message: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    finished_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def to_dict(self) -> JsonDict:
        """转换为可写入进度日志的字典。"""

        return {
            "source": self.source,
            "status": self.status,
            "rate_key": self.rate_key,
            "complexity": self.complexity,
            "row_count": self.row_count,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }


@dataclass(frozen=True)
class ProviderChainResult:
    """Provider 链整体执行结果。"""

    status: ProviderAttemptStatus
    source: str | None
    payload: Any = None
    row_count: int = 0
    attempts: tuple[ProviderAttempt, ...] = ()
    error_message: str | None = None

    def to_dict(self) -> JsonDict:
        """转换为可序列化结果。"""

        return {
            "status": self.status,
            "source": self.source,
            "payload": self.payload,
            "row_count": self.row_count,
            "error_message": self.error_message,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


class ProviderChain:
    """按顺序尝试多个 Provider，并返回结构化降级结果。"""

    def __init__(self, sources: Sequence[ProviderSource]) -> None:
        if not sources:
            raise ValueError("ProviderChain 至少需要一个数据源")
        self.sources = tuple(sources)

    def run(
        self,
        fetch: Callable[[ProviderSource], ProviderFetchResult],
    ) -> ProviderChainResult:
        """执行 Provider 降级链。

        fetch 负责真正访问数据源。该方法负责捕获异常、记录尝试、遇到成功后停止。
        """

        attempts: list[ProviderAttempt] = []
        last_error: str | None = None
        for source in self.sources:
            started_at = datetime.now(tz=UTC)
            try:
                fetch_result = fetch(source)
            except Exception as exc:  # noqa: BLE001 - Provider 边界需要结构化保留失败
                fetch_result = ProviderFetchResult.error(str(exc))
            finished_at = datetime.now(tz=UTC)
            attempt = ProviderAttempt(
                source=source.name,
                status=fetch_result.status,
                rate_key=source.rate_key,
                complexity=source.complexity,
                row_count=fetch_result.row_count,
                error_message=fetch_result.error_message,
                started_at=started_at,
                finished_at=finished_at,
            )
            attempts.append(attempt)
            if fetch_result.status == "ok":
                return ProviderChainResult(
                    status="ok",
                    source=source.name,
                    payload=fetch_result.payload,
                    row_count=fetch_result.row_count,
                    attempts=tuple(attempts),
                )
            last_error = fetch_result.error_message
        return ProviderChainResult(
            status="error",
            source=None,
            attempts=tuple(attempts),
            error_message=last_error,
        )


@dataclass(frozen=True)
class TaskItem:
    """一个可独立同步的任务 item。"""

    item_id: str
    data_domain: str
    market: str
    asset_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    provider: str | None = None
    payload: JsonDict = field(default_factory=dict)
    status: TaskItemStatus = "pending"
    item_count: int = 0
    error_message: str | None = None
    attempts: tuple[ProviderAttempt, ...] = ()
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def mark_running(self) -> "TaskItem":
        """标记 item 正在处理。"""

        return replace(self, status="running", updated_at=datetime.now(tz=UTC))

    def mark_completed(
        self,
        *,
        item_count: int = 0,
        attempts: Sequence[ProviderAttempt] | None = None,
    ) -> "TaskItem":
        """标记 item 已完成。"""

        return replace(
            self,
            status="completed",
            item_count=max(0, int(item_count)),
            error_message=None,
            attempts=tuple(attempts if attempts is not None else self.attempts),
            updated_at=datetime.now(tz=UTC),
        )

    def mark_failed(
        self,
        error_message: str,
        *,
        attempts: Sequence[ProviderAttempt] | None = None,
    ) -> "TaskItem":
        """标记 item 失败。"""

        return replace(
            self,
            status="failed",
            error_message=str(error_message),
            attempts=tuple(attempts if attempts is not None else self.attempts),
            updated_at=datetime.now(tz=UTC),
        )

    def mark_skipped(self, reason: str) -> "TaskItem":
        """标记 item 被跳过。"""

        return replace(
            self,
            status="skipped",
            error_message=str(reason),
            updated_at=datetime.now(tz=UTC),
        )

    def to_dict(self) -> JsonDict:
        """转换为可写入进度快照的字典。"""

        return {
            "item_id": self.item_id,
            "data_domain": self.data_domain,
            "market": self.market,
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "provider": self.provider,
            "payload": dict(self.payload),
            "status": self.status,
            "item_count": self.item_count,
            "error_message": self.error_message,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class TaskRun:
    """一次任务运行的内存态汇总。"""

    job_name: str
    run_id: str
    items: list[TaskItem] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def add_item(self, item: TaskItem) -> None:
        """追加或替换一个 item 的当前状态。"""

        for index, existing in enumerate(self.items):
            if existing.item_id == item.item_id:
                self.items[index] = item
                self.updated_at = datetime.now(tz=UTC)
                return
        self.items.append(item)
        self.updated_at = datetime.now(tz=UTC)

    def snapshot(self) -> JsonDict:
        """生成前端进度快照。"""

        counts = self._status_counts()
        total_items = len(self.items)
        finished_items = (
            counts["completed"]
            + counts["failed"]
            + counts["skipped"]
            + counts["cancelled"]
        )
        return {
            "job_name": self.job_name,
            "run_id": self.run_id,
            "total_items": total_items,
            "pending_items": counts["pending"],
            "running_items": counts["running"],
            "completed_items": counts["completed"],
            "failed_items": counts["failed"],
            "skipped_items": counts["skipped"],
            "cancelled_items": counts["cancelled"],
            "remaining_items": max(0, total_items - finished_items),
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "items": [item.to_dict() for item in self.items],
        }

    def _status_counts(self) -> dict[str, int]:
        """统计 item 状态数量。"""

        counts = {
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": 0,
        }
        for item in self.items:
            counts[item.status] += 1
        return counts
