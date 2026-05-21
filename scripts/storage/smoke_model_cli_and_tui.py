"""验证模型配置、路由预览、检索配置和轻量 TUI 入口。"""

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

        smoke_provider_key = "smoke-model-provider"
        smoke_review_provider_key = "smoke-review-provider"
        smoke_primary_model_key = "smoke-primary-model"
        smoke_review_model_key = "smoke-review-model"
        smoke_workflow_type = "smoke_model_config"
        smoke_decision_type = "smoke_decision"
        smoke_retrieval_profile = "smoke_finance_memory"

        run_json(
            [
                "models",
                "set-provider",
                "--provider-key",
                smoke_provider_key,
                "--provider-vendor",
                "deepseek",
                "--provider-name",
                "Smoke DeepSeek",
                "--base-url",
                "https://api.deepseek.example/v1",
                "--api-key",
                "smoke-primary-api-key",
            ]
        )
        run_json(
            [
                "models",
                "set-provider",
                "--provider-key",
                smoke_review_provider_key,
                "--provider-vendor",
                "openai",
                "--provider-name",
                "Smoke OpenAI",
                "--base-url",
                "https://api.openai.example/v1",
                "--api-key",
                "smoke-review-api-key",
            ]
        )
        run_json(
            [
                "models",
                "set-model",
                "--provider-key",
                smoke_provider_key,
                "--model-key",
                smoke_primary_model_key,
                "--model-name",
                "Smoke Primary Model",
                "--role",
                "primary_financial_analyst",
                "--route-priority",
                "999",
            ]
        )
        run_json(
            [
                "models",
                "set-model",
                "--provider-key",
                smoke_review_provider_key,
                "--model-key",
                smoke_review_model_key,
                "--model-name",
                "Smoke Review Model",
                "--role",
                "high_risk_reviewer",
                "--route-priority",
                "998",
            ]
        )
        run_json(
            [
                "models",
                "set-route",
                "--workflow-type",
                smoke_workflow_type,
                "--task",
                "roundtable_discussion",
                "--role",
                "primary_financial_analyst",
                "--decision-type",
                smoke_decision_type,
                "--model-key",
                smoke_primary_model_key,
                "--priority",
                "999",
                "--reason",
                "冒烟验证常规分析在线路由。",
            ]
        )
        run_json(
            [
                "models",
                "set-route",
                "--workflow-type",
                smoke_workflow_type,
                "--task",
                "high_risk_review",
                "--role",
                "high_risk_reviewer",
                "--decision-type",
                smoke_decision_type,
                "--model-key",
                smoke_review_model_key,
                "--priority",
                "999",
                "--reason",
                "冒烟验证高风险复核在线路由。",
            ]
        )
        run_json(
            [
                "models",
                "set-retrieval",
                "--profile-key",
                smoke_retrieval_profile,
                "--profile-name",
                "冒烟 Finance Memory 检索配置",
                "--usage-scope",
                "finance_memory",
                "--search-method",
                "hybrid_search",
                "--embedding-model-key",
                smoke_primary_model_key,
                "--rerank-model-key",
                smoke_review_model_key,
                "--top-k",
                "9",
                "--score-threshold",
                "0.120000",
                "--reranking-enable",
                "--semantic-weight",
                "0.7",
                "--keyword-weight",
                "0.3",
                "--default",
            ]
        )

        db_config_payload = run_json(["models", "config"])
        if db_config_payload["data"]["source"] != "database":
            raise AssertionError("模型配置中心应优先从数据库加载。")
        db_models = db_config_payload["data"]["models"]
        if smoke_primary_model_key not in db_models:
            raise AssertionError("数据库模型配置必须包含在线写入的模型实例。")
        db_profiles = db_config_payload["data"]["retrieval_profiles"]
        if db_profiles[smoke_retrieval_profile]["top_k"] != 9:
            raise AssertionError("数据库检索配置必须进入模型注册表摘要。")

        db_route_payload = run_json(
            [
                "models",
                "route-preview",
                "--workflow-type",
                smoke_workflow_type,
                "--task",
                "roundtable_discussion",
                "--asset-id",
                "asset:smoke:model_config_db",
                "--decision-type",
                smoke_decision_type,
                "--high-risk",
            ]
        )
        db_route_keys = {route["model_key"] for route in db_route_payload["data"]["routes"]}
        if db_route_keys != {smoke_primary_model_key, smoke_review_model_key}:
            raise AssertionError(f"数据库路由规则必须覆盖默认模型，实际={db_route_keys}")

        db_retrieval_payload = run_json(["models", "retrieval"])
        profile_keys = {
            profile["profile_key"]
            for profile in db_retrieval_payload["data"]["retrieval_profiles"]
        }
        if smoke_retrieval_profile not in profile_keys:
            raise AssertionError("models retrieval 必须能列出在线写入的检索配置。")

        tui_retrieval_process = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_agent.cli",
                "models",
                "tui",
                "--scripted",
                "retrieval",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            text=True,
        )
        if smoke_retrieval_profile not in tui_retrieval_process.stdout:
            raise AssertionError("脚本化 TUI 必须能展示数据库检索配置。")

    print(
        {
            "configured_models": sorted(models),
            "route_keys": sorted(route_keys),
            "db_route_keys": sorted(db_route_keys),
            "retrieval_profile": smoke_retrieval_profile,
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
