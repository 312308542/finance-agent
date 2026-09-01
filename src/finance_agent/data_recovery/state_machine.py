"""补跑批次状态机的合法转换表。

规格 9.4：

draft -> approved -> running -> verifying -> completed / completed_with_exceptions
running <-> paused
running / verifying -> attention_required -> running
任意非终态 -> cancelled

所有状态写入必须经过本模块校验，非法跳转直接抛出
`InvalidRecoveryTransition`，保证暂停/继续/取消和人工关注路径可控。
"""

from __future__ import annotations

from finance_agent.data_recovery.models import TERMINAL_RUN_STATUSES


class InvalidRecoveryTransition(ValueError):
    """批次状态发生规格之外的跳转。"""

    def __init__(self, current: str, next_status: str) -> None:
        self.current = current
        self.next_status = next_status
        super().__init__(f"不允许的状态跳转: {current} -> {next_status}")


RUN_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("approved", "cancelled"),
    "approved": ("running", "cancelled"),
    "running": ("paused", "verifying", "attention_required", "cancelled"),
    "paused": ("running", "cancelled"),
    "verifying": (
        "completed",
        "completed_with_exceptions",
        "attention_required",
        "cancelled",
    ),
    "attention_required": ("running", "cancelled"),
    "completed": (),
    "completed_with_exceptions": (),
    "cancelled": (),
}


def assert_transition(current: str, next_status: str) -> None:
    """校验一次状态跳转是否合法，不合法时抛出异常。"""

    if current in TERMINAL_RUN_STATUSES:
        raise InvalidRecoveryTransition(current, next_status)
    allowed = RUN_TRANSITIONS.get(current)
    if allowed is None:
        raise InvalidRecoveryTransition(current, next_status)
    if next_status not in allowed:
        raise InvalidRecoveryTransition(current, next_status)


def can_transition(current: str, next_status: str) -> bool:
    """`assert_transition` 的布尔版本，供查询与展示逻辑使用。"""

    try:
        assert_transition(current, next_status)
    except InvalidRecoveryTransition:
        return False
    return True


def gate_status_for_run(run_status: str, *, has_blocking_gaps: bool) -> str:
    """根据批次状态推导门控状态（规格 12.1/13.1）。

    - running / verifying：补跑正在执行，门控 recovering。
    - draft 等待确认、paused、attention_required、取消后核心缺口仍在：degraded。
    - 仅存在非阻塞缺口的提醒草稿不阻止派生任务：open。
    - 终态 completed*：验收通过，open。
    """

    if run_status in {"running", "verifying", "approved"}:
        return "recovering"
    if run_status in {"draft", "paused", "attention_required", "cancelled"}:
        return "degraded" if has_blocking_gaps else "open"
    if run_status in {"completed", "completed_with_exceptions"}:
        return "open"
    return "degraded"
