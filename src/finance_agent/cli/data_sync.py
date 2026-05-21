"""数据同步配置 CLI。

这个模块只负责把数据同步预设、预览、校验和导出暴露给命令行层，
不直接访问行情、不直接执行采集。后续页面和 MCP 也应该复用这里的
配置模型。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from finance_agent.data.sync_config import (
    DataSyncConfig,
    build_preset_config,
    export_scheduler_payload,
    load_data_sync_config,
    normalize_markets,
    normalize_preset,
    preview_data_sync_config,
    save_data_sync_config,
    validate_data_sync_config,
)
from finance_agent.data.sync_tui import render_data_sync_tui

JsonDict = dict[str, Any]


def add_data_arguments(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册 `finance-agent data` 命令组。"""

    data = subparsers.add_parser("data", help="基础数据同步配置与预览。")
    data_commands = data.add_subparsers(dest="command", required=True)

    config = data_commands.add_parser("config", help="数据同步配置中心。")
    config_commands = config.add_subparsers(dest="subcommand", required=True)

    init = config_commands.add_parser("init", help="生成数据同步配置模板。")
    init.add_argument(
        "--preset",
        default="personal-comprehensive",
        help="配置预设：personal-comprehensive、ashare-comprehensive、crypto-comprehensive、lightweight。",
    )
    init.add_argument(
        "--markets",
        default=None,
        help="逗号分隔的市场列表，例如 ashare,crypto_spot,crypto_future。",
    )
    init.add_argument(
        "--output",
        default="data_sync_config.json",
        help="输出 JSON 文件路径，默认 data_sync_config.json。",
    )

    preview = config_commands.add_parser("preview", help="预览数据同步任务。")
    preview.add_argument(
        "--config-file",
        default=None,
        help="数据同步配置 JSON 文件路径；未提供时使用默认全面模式。",
    )

    validate = config_commands.add_parser("validate", help="校验数据同步配置。")
    validate.add_argument(
        "--config-file",
        default=None,
        help="数据同步配置 JSON 文件路径；未提供时使用默认全面模式。",
    )

    export = config_commands.add_parser("export", help="导出底层调度器任务计划。")
    export.add_argument(
        "--config-file",
        default=None,
        help="数据同步配置 JSON 文件路径；未提供时使用默认全面模式。",
    )
    export.add_argument(
        "--output",
        default=None,
        help="可选：将导出的任务计划写入 JSON 文件。",
    )

    tui = config_commands.add_parser("tui", help="打开轻量文本配置 TUI。")
    tui.add_argument(
        "--config-file",
        default=None,
        help="数据同步配置 JSON 文件路径；未提供时使用默认全面模式。",
    )
    tui.add_argument(
        "--scripted",
        choices=["preview", "validate", "export"],
        default=None,
        help="脚本化 TUI 动作，供 smoke 和自动化测试使用。",
    )


def dispatch_data(args: argparse.Namespace) -> JsonDict:
    """执行数据同步配置命令。"""

    if args.command != "config":
        raise ValueError(f"未知 data 命令：{args.command}")

    if args.subcommand == "init":
        config = build_config_from_args(args)
        return {
            "status": "ok",
            "data": init_config(config=config, output=args.output),
        }
    if args.subcommand == "preview":
        config = load_data_sync_config(args.config_file)
        preview = preview_data_sync_config(config)
        return {
            "status": "ok",
            "data": {
                "config": config.to_dict(),
                "preview": preview,
                "tasks": preview["tasks"],
                "manual_symbol_required": preview["manual_symbol_required"],
                "validation": preview["validation"],
                "enabled_markets": preview["enabled_markets"],
            },
        }
    if args.subcommand == "validate":
        config = load_data_sync_config(args.config_file)
        result = validate_data_sync_config(config)
        return {
            "status": "ok",
            "data": {
                "config": config.to_dict(),
                "validation": result.to_dict(),
                "task_count": result.task_count,
                "enabled_market_count": result.enabled_market_count,
                "valid": result.valid,
                "errors": result.errors,
                "warnings": result.warnings,
            },
        }
    if args.subcommand == "export":
        config = load_data_sync_config(args.config_file)
        payload = export_scheduler_payload(config)
        if args.output:
            Path(args.output).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return {
            "status": "ok",
            "data": {
                "config": config.to_dict(),
                "scheduler_payload": payload,
                "jobs": payload["jobs"],
                "written_to": args.output,
            },
        }
    if args.subcommand == "tui":
        config = load_data_sync_config(args.config_file)
        output = render_data_sync_tui(config=config, scripted=args.scripted)
        return {
            "status": "ok",
            "data": {
                "config": config.to_dict(),
                "output": output,
            },
        }
    raise ValueError(f"未知 data config 命令：{args.subcommand}")


def build_config_from_args(args: argparse.Namespace) -> DataSyncConfig:
    """根据 CLI 参数构建数据同步配置。"""

    preset = normalize_preset(args.preset)
    markets = normalize_markets(parse_market_arg(args.markets))
    config = build_preset_config(preset, markets=markets or None)
    if markets:
        market_payload = {
            market: config.markets[market]
            for market in markets
            if market in config.markets
        }
        config = replace(config, markets=market_payload)
    return config


def init_config(*, config: DataSyncConfig, output: str) -> JsonDict:
    """保存配置模板并返回摘要。"""

    saved_path = save_data_sync_config(config, output)
    preview = preview_data_sync_config(config)
    return {
        "preset": config.preset,
        "preset_label": preview["preset_label"],
        "config_path": str(saved_path),
        "config": config.to_dict(),
        "preview": preview,
        "scheduler_payload": export_scheduler_payload(config),
    }


def parse_market_arg(value: str | None) -> list[str]:
    """把市场参数拆成列表。"""

    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
