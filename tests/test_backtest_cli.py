from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from finance_agent.cli import main as cli_main


def test_backtest_cli_parser_accepts_run_arguments() -> None:
    """CLI 应支持方案 09 约定的 factor_score_topn 回测命令。"""

    args = cli_main.build_parser().parse_args(
        [
            "backtest",
            "run",
            "--strategy",
            "factor_score_topn",
            "--universe",
            "universe:merged:ashare:recommendation",
            "--strategy-id",
            "strategy:ashare:short_swing",
            "--years",
            "5",
            "--score-mode",
            "replayed",
            "--topn",
            "20",
            "--rebalance",
            "once",
        ]
    )

    assert args.group == "backtest"
    assert args.command == "run"
    assert args.strategy == "factor_score_topn"
    assert args.universe == "universe:merged:ashare:recommendation"
    assert args.strategy_id == "strategy:ashare:short_swing"
    assert args.years == 5
    assert args.score_mode == "replayed"
    assert args.topn == 20
    assert args.rebalance == "once"


def test_backtest_cli_dispatches_run_command_with_session(monkeypatch: Any) -> None:
    """CLI 分发层应把 backtest run 交给回测入口，并复用统一数据库 session。"""

    args = cli_main.build_parser().parse_args(
        [
            "--database-url",
            "postgresql://example/test",
            "backtest",
            "run",
            "--strategy",
            "factor_score_topn",
            "--universe",
            "universe:merged:ashare:recommendation",
            "--strategy-id",
            "strategy:ashare:short_swing",
        ]
    )
    calls: list[dict[str, Any]] = []
    fake_session = object()

    @contextmanager
    def fake_session_scope(session_factory: Any) -> Iterator[object]:
        calls.append({"kind": "session_scope", "session_factory": session_factory})
        yield fake_session

    def fake_dispatch_backtest(session: object, parsed_args: Any) -> dict[str, Any]:
        calls.append(
            {
                "kind": "dispatch_backtest",
                "session": session,
                "strategy": parsed_args.strategy,
                "universe": parsed_args.universe,
                "strategy_id": parsed_args.strategy_id,
            }
        )
        return {
            "status": "ok",
            "data": {
                "backtest_id": "bt:ashare:short_swing",
                "status": "available",
            },
        }

    monkeypatch.setattr(cli_main, "create_session_factory", lambda database_url: f"factory:{database_url}")
    monkeypatch.setattr(cli_main, "session_scope", fake_session_scope)
    monkeypatch.setattr(cli_main, "dispatch_backtest", fake_dispatch_backtest, raising=False)

    result = cli_main.dispatch(args)

    assert result["data"]["backtest_id"] == "bt:ashare:short_swing"
    assert calls == [
        {
            "kind": "session_scope",
            "session_factory": "factory:postgresql://example/test",
        },
        {
            "kind": "dispatch_backtest",
            "session": fake_session,
            "strategy": "factor_score_topn",
            "universe": "universe:merged:ashare:recommendation",
            "strategy_id": "strategy:ashare:short_swing",
        },
    ]
