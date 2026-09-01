"""A 股停跑恢复补跑模块。

对外只暴露 `DataRecoveryModule` 门面与只读数据结构；缺口规则、任务分区、
状态持久化、事实验收和门控细节全部隐藏在包内。调度器、HTTP、CLI 和 MCP
都不得绕过门面直接操作补跑内部状态。
"""

from __future__ import annotations

from finance_agent.data_recovery.gate import BLOCKED_BY_RECOVERY, RecoveryGate
from finance_agent.data_recovery.service import DataRecoveryModule, StalePlanError

__all__ = [
    "BLOCKED_BY_RECOVERY",
    "DataRecoveryModule",
    "RecoveryGate",
    "StalePlanError",
]
