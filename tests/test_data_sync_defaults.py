from finance_agent.api.schemas import DataSyncConfigUpdateRequest
from finance_agent.cli.main import build_parser


def test_cli_config_init_defaults_to_personal_ashare() -> None:
    """CLI 未指定预设时应生成 A 股与基金默认配置。"""

    args = build_parser().parse_args(["data", "config", "init"])

    assert args.preset == "personal-ashare"


def test_api_config_request_defaults_to_personal_ashare_markets() -> None:
    """API 保存请求的默认预设和市场必须保持一致。"""

    request = DataSyncConfigUpdateRequest()

    assert request.preset == "personal-ashare"
    assert request.markets == ["ashare", "fund"]
