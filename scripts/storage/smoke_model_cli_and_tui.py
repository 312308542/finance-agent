"""验证模型配置、路由预览、dry-run 测试和轻量 TUI 入口。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    """执行模型 CLI/TUI 冒烟验证。"""

    config = {
        "models": {
            "deepseek-v4-pro": {
                "provider": "deepseek",
                "model_name": "DeepSeek V4 Pro",
                "base_url": "https://api.deepseek.example/v1",
                "api_key": "deepseek-smoke-key",
                "role": "primary_financial_analyst",
                "enabled": True,
            },
            "gpt-5.5-pro": {
                "provider": "openai",
                "model_name": "GPT-5.5 Pro",
                "base_url": "https://api.openai.example/v1",
                "api_key": "openai-smoke-key",
                "role": "high_risk_reviewer",
                "enabled": True,
            },
        }
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        config_file = Path(temp_dir) / "models.json"
        config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        config_payload = run_json(
            [
                "models",
                "config",
                "--config-file",
                str(config_file),
            ]
        )
        models = config_payload["data"]["models"]
        if models["deepseek-v4-pro"]["provider"] != "deepseek":
            raise AssertionError("模型配置必须能读取 DeepSeek 配置。")
        if models["deepseek-v4-pro"]["api_key"] != "dee...key":
            raise AssertionError("模型配置输出必须脱敏 API key。")
        if not models["gpt-5.5-pro"]["ready"]:
            raise AssertionError("有 base_url 和 api_key 的模型应标记为 ready。")

        route_payload = run_json(
            [
                "models",
                "route-preview",
                "--config-file",
                str(config_file),
                "--workflow-type",
                "recommendation_decision",
                "--task",
                "roundtable_discussion",
                "--asset-id",
                "asset:smoke:model_cli",
                "--decision-type",
                "swap_candidate",
                "--high-risk",
            ]
        )
        routes = route_payload["data"]["routes"]
        route_keys = {route["model_key"] for route in routes}
        if route_keys != {"deepseek-v4-pro", "gpt-5.5-pro"}:
            raise AssertionError(f"高风险路由预览必须包含主模型和复核模型，实际={route_keys}")
        if not all(route["configured"] for route in routes):
            raise AssertionError("路由预览必须标记模型已配置。")

        test_payload = run_json(
            [
                "models",
                "test",
                "--config-file",
                str(config_file),
                "--model-key",
                "deepseek-v4-pro",
                "--prompt",
                "用一句中文说明当前只做 dry-run。",
                "--dry-run",
            ]
        )
        test_data = test_payload["data"]
        if not test_data["dry_run"]:
            raise AssertionError("模型测试 dry-run 不应发起真实 HTTP 请求。")
        if test_data["request"]["model"] != "DeepSeek V4 Pro":
            raise AssertionError("dry-run 请求必须使用配置中的模型名。")
        if "deepseek-smoke-key" in json.dumps(test_payload, ensure_ascii=False):
            raise AssertionError("模型测试输出不能泄露 API key。")

        tui_process = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_agent.cli",
                "models",
                "tui",
                "--config-file",
                str(config_file),
                "--scripted",
                "config",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            text=True,
        )
        if "模型配置" not in tui_process.stdout or "deepseek-v4-pro" not in tui_process.stdout:
            raise AssertionError("脚本化 TUI 必须能展示模型配置。")

    print(
        {
            "configured_models": sorted(models),
            "route_keys": sorted(route_keys),
            "dry_run": test_data["dry_run"],
            "tui_status": "ok",
        }
    )


def run_json(args: list[str]) -> dict[str, object]:
    """运行 finance-agent CLI 并解析 JSON。"""

    process = subprocess.run(
        [sys.executable, "-m", "finance_agent.cli", *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    payload = json.loads(process.stdout)
    if payload["status"] != "ok":
        raise AssertionError(f"CLI 返回非 ok：{payload}")
    return payload


if __name__ == "__main__":
    main()
