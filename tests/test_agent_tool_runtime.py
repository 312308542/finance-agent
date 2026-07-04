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


class _FakeProfileService:
    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []

    def get_profile(self, *, owner_id: str) -> dict[str, object]:
        return {
            "profile_id": f"profile:{owner_id}",
            "owner_id": owner_id,
            "risk_appetite": "balanced",
        }

    def upsert_profile(
        self,
        *,
        owner_id: str,
        updates: dict[str, object],
        source: dict[str, object],
        evidence: list[dict[str, object]],
    ) -> dict[str, object]:
        self.upserts.append(
            {
                "owner_id": owner_id,
                "updates": updates,
                "source": source,
                "evidence": evidence,
            }
        )
        return self.get_profile(owner_id=owner_id)


class _FakeAdviceService:
    def suggest_style(self, *, owner_id: str) -> dict[str, object]:
        return {
            "owner_id": owner_id,
            "suggested_risk_appetite": "conservative",
            "deterministic_fields_unchanged": [
                "asset_scores.total_score",
                "signal_snapshots.direction",
                "risk_findings.severity",
            ],
        }


def test_profile_tools_are_registered_with_controlled_write_boundary() -> None:
    """画像工具进入运行时，其中 upsert 必须显式声明为受控写工具。"""

    runtime = FinanceToolRuntime.__new__(FinanceToolRuntime)
    runtime._tools = {}
    runtime.profile_service = _FakeProfileService()
    runtime.profile_advice = _FakeAdviceService()

    FinanceToolRuntime._register_profile_tools(runtime)

    assert runtime.get_tool("profile.get").read_only is True
    assert runtime.get_tool("advice.suggest_style").read_only is True
    assert runtime.get_tool("profile.upsert").read_only is False
    assert runtime.get_tool("profile.upsert").requires_review is True
    assert runtime.get_tool("profile.upsert").write_scope == "investment_profile"


def test_profile_tool_calls_delegate_to_profile_services() -> None:
    """画像工具只编排画像服务，不直接修改评分、信号或风险事实。"""

    runtime = FinanceToolRuntime.__new__(FinanceToolRuntime)
    runtime.profile_service = _FakeProfileService()
    runtime.profile_advice = _FakeAdviceService()

    profile = FinanceToolRuntime.get_profile(runtime, owner_id="owner:demo")
    updated = FinanceToolRuntime.upsert_profile(
        runtime,
        owner_id="owner:demo",
        updates={"risk_appetite": "conservative"},
        source={"risk_appetite": "elicited"},
        evidence=[{"type": "chat", "id": "chat:1"}],
    )
    advice = FinanceToolRuntime.suggest_profile_style(runtime, owner_id="owner:demo")

    assert profile["profile_id"] == "profile:owner:demo"
    assert updated["owner_id"] == "owner:demo"
    assert runtime.profile_service.upserts[0]["source"] == {"risk_appetite": "elicited"}
    assert advice["deterministic_fields_unchanged"] == [
        "asset_scores.total_score",
        "signal_snapshots.direction",
        "risk_findings.severity",
    ]


class _FakeIndicators:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_latest_indicator_frame(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["horizon"] == "smc_lite_v2":
            return type(
                "Frame",
                (),
                {
                    "indicator_frame_id": "struct:smc:1",
                    "asset_id": "ashare:600519",
                    "symbol": "600519",
                    "market": "ashare",
                    "timeframe": "1d",
                    "horizon": "smc_lite_v2",
                    "library": "structural-lite",
                    "library_version": "2026.07.04",
                    "input_start_at": None,
                    "input_end_at": None,
                    "bar_count": 250,
                    "rsi_14": None,
                    "macd": None,
                    "macd_signal": None,
                    "macd_hist": None,
                    "atr_14": None,
                    "bb_percent_b": None,
                    "ma_20": None,
                    "ma_60": None,
                    "status": "available",
                    "as_of": None,
                    "payload": {
                        "schema_version": "smc_lite_v2",
                        "status": "available",
                        "evidence_id": "smc_lite:ashare:600519:1d:20260704T070000Z",
                        "red_lines": ["LLM 只能解读结构引擎输出。"],
                    },
                },
            )()
        return None


def test_structure_tool_is_registered_and_reads_structural_lite_frames() -> None:
    """结构证据工具只读 `indicator_frames` 中的 structural-lite 输出。"""

    runtime = FinanceToolRuntime.__new__(FinanceToolRuntime)
    runtime._tools = {}
    runtime.indicators = _FakeIndicators()
    runtime.profile_service = _FakeProfileService()
    runtime.profile_advice = _FakeAdviceService()

    FinanceToolRuntime._register_builtin_tools(runtime)
    result = runtime.call("structure.get_asset_context", asset_id="ashare:600519")

    assert runtime.get_tool("structure.get_asset_context").read_only is True
    assert result["library"] == "structural-lite"
    assert result["structure_frames"][0]["horizon"] == "smc_lite_v2"
    assert result["structure_frames"][0]["payload"]["evidence_id"].startswith("smc_lite:")
    assert [call["horizon"] for call in runtime.indicators.calls] == [
        "structural_swings_v2",
        "smc_lite_v2",
        "harmonic_lite_v2",
        "elliott_lite_v2",
    ]
