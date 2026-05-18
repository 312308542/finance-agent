"""验证 FinanceAssistantService 兼容入口。"""

from __future__ import annotations

from finance_agent.agents import FinanceAssistantService
from finance_agent.agents.personal_assistant import PersonalFinanceAgentService


def main() -> None:
    """验证新类名仍兼容旧服务入口。"""

    if not issubclass(FinanceAssistantService, PersonalFinanceAgentService):
        raise AssertionError("FinanceAssistantService 必须兼容 PersonalFinanceAgentService")
    print(
        {
            "service": "FinanceAssistantService",
            "compatible_with": "PersonalFinanceAgentService",
        }
    )


if __name__ == "__main__":
    main()
