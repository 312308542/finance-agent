"""轻量数据同步配置 TUI。

当前 TUI 先提供脚本化预览、校验和预设展示能力，交互式输入后续可以继续
沿用本模块的渲染函数。它不直接访问数据库，也不发起采集。
"""

from __future__ import annotations

from collections.abc import Callable

from finance_agent.data.sync_config import (
    DataSyncConfig,
    export_scheduler_payload,
    preset_label,
    preview_data_sync_config,
    validate_data_sync_config,
)


def render_data_sync_tui(
    *,
    config: DataSyncConfig,
    scripted: str | None = None,
    input_func: Callable[[str], str] = input,
) -> str:
    """渲染轻量数据同步配置 TUI。"""

    if scripted:
        return render_scripted_tui(config=config, action=scripted)

    lines = [
        "finance-agent 数据同步配置 TUI",
        "1. 查看当前配置预览",
        "2. 校验当前配置",
        "3. 查看底层任务计划",
        "q. 退出",
    ]
    choice = input_func("\n".join(lines) + "\n请选择: ").strip().lower()
    if choice == "1":
        return render_config_preview(config)
    if choice == "2":
        return render_validation(config)
    if choice == "3":
        return render_scheduler_plan(config)
    return "已退出数据同步配置 TUI。"


def render_scripted_tui(*, config: DataSyncConfig, action: str) -> str:
    """执行脚本化 TUI 动作，供 smoke 和自动化验证使用。"""

    if action == "preview":
        return render_config_preview(config)
    if action == "validate":
        return render_validation(config)
    if action == "export":
        return render_scheduler_plan(config)
    raise ValueError(f"未知数据同步 TUI scripted 动作：{action}")


def render_config_preview(config: DataSyncConfig) -> str:
    """渲染配置预览。"""

    preview = preview_data_sync_config(config)
    lines = [
        "finance-agent 数据同步配置 TUI",
        f"模式：{preset_label(config.preset)}",
        f"资源策略：{config.resource_profile}",
        f"缓存后端：{config.cache_backend}",
        f"需要手填股票：{'是' if preview['manual_symbol_required'] else '否'}",
        "启用市场：",
    ]
    for market in preview["enabled_markets"]:
        lines.append(f"- {market}")
    lines.append("任务预览：")
    for task in preview["tasks"]:
        lines.append(
            f"- {task['task_key']}：{task['title']} / "
            f"{task['mode']} / {task['interval_seconds']} 秒"
        )
    validation = preview["validation"]
    if validation["warnings"]:
        lines.append("提示：")
        lines.extend(f"- {item}" for item in validation["warnings"])
    return "\n".join(lines)


def render_validation(config: DataSyncConfig) -> str:
    """渲染校验结果。"""

    result = validate_data_sync_config(config)
    lines = [
        "数据同步配置校验",
        f"状态：{'通过' if result.valid else '失败'}",
        f"启用市场数：{result.enabled_market_count}",
        f"任务数：{result.task_count}",
    ]
    if result.errors:
        lines.append("错误：")
        lines.extend(f"- {item}" for item in result.errors)
    if result.warnings:
        lines.append("提示：")
        lines.extend(f"- {item}" for item in result.warnings)
    return "\n".join(lines)


def render_scheduler_plan(config: DataSyncConfig) -> str:
    """渲染底层任务计划摘要。"""

    payload = export_scheduler_payload(config)
    lines = [
        "数据同步任务计划",
        f"任务数：{len(payload['jobs'])}",
    ]
    for job in payload["jobs"]:
        lines.append(
            f"- {job['name']}：{job['group']} / {job['market']} / "
            f"{job['interval_seconds']} 秒"
        )
    return "\n".join(lines)
