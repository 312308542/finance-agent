"""纯标准库模型配置 TUI。

这里的 TUI 是 CLI 内的轻量文本菜单，用于本地配置和测试阶段；它不引入
Textual/Rich 依赖，也不接管业务 Workflow。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from finance_agent.agents.runtime.model_config import (
    ModelRegistry,
    preview_model_routes,
    test_model_endpoint,
)
from finance_agent.storage.repositories import ModelRuntimeConfigRepository


def render_model_tui(
    *,
    registry: ModelRegistry,
    model_config_repository: ModelRuntimeConfigRepository | None = None,
    scripted: str | None = None,
    input_func: Callable[[str], str] = input,
) -> str:
    """渲染轻量模型 TUI；scripted 用于 smoke 和自动化验证。"""

    if scripted:
        return render_scripted_tui(
            registry=registry,
            action=scripted,
            model_config_repository=model_config_repository,
        )

    lines = [
        "finance-agent 模型配置 TUI",
        "1. 查看模型配置",
        "2. 预览推荐决策路由",
        "3. dry-run 测试 DeepSeek V4 Pro",
        "4. 查看检索配置",
        "q. 退出",
    ]
    choice = input_func("\n".join(lines) + "\n请选择: ").strip().lower()
    if choice == "1":
        return render_model_config(registry)
    if choice == "2":
        return render_route_preview(
            registry,
            model_config_repository=model_config_repository,
        )
    if choice == "3":
        return render_model_test(registry)
    if choice == "4":
        return render_retrieval_profiles(registry)
    return "已退出模型配置 TUI。"


def render_scripted_tui(
    *,
    registry: ModelRegistry,
    action: str,
    model_config_repository: ModelRuntimeConfigRepository | None = None,
) -> str:
    """执行脚本化 TUI 动作。"""

    if action == "config":
        return render_model_config(registry)
    if action == "route-preview":
        return render_route_preview(
            registry,
            model_config_repository=model_config_repository,
        )
    if action == "test":
        return render_model_test(registry)
    if action == "retrieval":
        return render_retrieval_profiles(registry)
    raise ValueError(f"未知 TUI scripted 动作：{action}")


def render_model_config(registry: ModelRegistry) -> str:
    """渲染模型配置。"""

    lines = [f"模型配置（来源：{registry.source}）"]
    for model_key, config in sorted(registry.models.items()):
        status = "ready" if config.ready else "not-ready"
        lines.append(
            f"- {model_key}: {config.provider} / {config.model_name} / "
            f"{status} / {config.base_url or '未配置 base_url'}"
        )
    if registry.retrieval_profiles:
        lines.append(f"检索配置：{len(registry.retrieval_profiles)} 个 profile")
    return "\n".join(lines)


def render_route_preview(
    registry: ModelRegistry,
    *,
    model_config_repository: ModelRuntimeConfigRepository | None = None,
) -> str:
    """渲染推荐决策路由预览。"""

    routes = preview_model_routes(
        registry=registry,
        workflow_type="recommendation_decision",
        task="roundtable_discussion",
        asset_id="asset:tui:preview",
        decision_type="swap_candidate",
        high_risk=True,
        model_config_repository=model_config_repository,
    )
    lines = ["模型路由预览"]
    for route in routes:
        ready = "ready" if route.get("ready") else "not-ready"
        lines.append(f"- {route['model_key']}: {route['role']} / {ready}")
    return "\n".join(lines)


def render_model_test(registry: ModelRegistry) -> str:
    """渲染 dry-run 测试摘要。"""

    result = test_model_endpoint(
        registry=registry,
        model_key="deepseek-v4-pro",
        prompt="用一句中文说明模型 dry-run 已就绪。",
        dry_run=True,
    )
    return "模型测试 dry-run\n" + json.dumps(
        {
            "model_key": result["model_key"],
            "status": result["status"],
            "model": result["request"]["model"],
        },
        ensure_ascii=False,
        indent=2,
    )


def render_retrieval_profiles(registry: ModelRegistry) -> str:
    """渲染检索配置摘要。"""

    lines = [f"检索配置（来源：{registry.source}）"]
    profiles = registry.retrieval_profiles or {}
    if not profiles:
        lines.append("- 未配置检索 profile")
        return "\n".join(lines)
    for profile_key, profile in sorted(profiles.items()):
        lines.append(
            f"- {profile_key}: {profile.get('usage_scope')} / "
            f"{profile.get('search_method')} / top_k={profile.get('top_k')}"
        )
    return "\n".join(lines)
