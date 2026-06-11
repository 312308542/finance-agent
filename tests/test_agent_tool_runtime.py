from sqlalchemy.exc import NoResultFound

from finance_agent.agents.tools.runtime import FinanceToolRuntime


class _MissingPortfolioService:
    def load_portfolio_snapshot(self, portfolio_id: str) -> object:
        raise NoResultFound("No row was found when one was required")


def test_get_portfolio_snapshot_returns_missing_when_portfolio_not_configured() -> None:
    """组合未配置时，事实工具返回结构化空快照，避免模型循环收到数据库异常。"""

    runtime = FinanceToolRuntime.__new__(FinanceToolRuntime)
    runtime.portfolios = _MissingPortfolioService()

    result = FinanceToolRuntime.get_portfolio_snapshot(
        runtime,
        portfolio_id="portfolio:default-owner:default",
    )

    assert result == {
        "portfolio": None,
        "positions": [],
        "status": "missing",
        "message": "组合未配置或不存在。",
        "portfolio_id": "portfolio:default-owner:default",
    }
