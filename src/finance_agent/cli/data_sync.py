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

from finance_agent.application.data_production_service import (
    DataBackfillPlanner,
    ProductionUniverseService,
)
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
from finance_agent.scoring.strategies import (
    default_scoring_strategy_seeds,
    validate_scoring_strategy_payload,
)
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import ScoringStrategyRepository, StrategyObservationRepository

JsonDict = dict[str, Any]


def add_data_arguments(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """注册 `finance-agent data` 命令组。"""

    data = subparsers.add_parser("data", help="基础数据同步配置与预览。")
    data_commands = data.add_subparsers(dest="command", required=True)


    recovery = data_commands.add_parser("recovery", help="A 股停跑恢复补跑批次。")
    recovery_commands = recovery.add_subparsers(dest="subcommand", required=True)

    recovery_preview = recovery_commands.add_parser(
        "preview", help="只读扫描缺口并生成或复用计划草稿。"
    )
    recovery_preview.add_argument("--requested-by", default=None, help="操作人标识。")

    recovery_list = recovery_commands.add_parser("list", help="列出补跑批次。")
    recovery_list.add_argument("--limit", type=int, default=20, help="返回条数上限。")

    recovery_status = recovery_commands.add_parser("status", help="查看批次状态。")
    recovery_status.add_argument("--run-id", required=True, help="补跑批次 ID。")

    recovery_approve = recovery_commands.add_parser(
        "approve", help="确认执行补跑；plan_hash 用于过期检测。"
    )
    recovery_approve.add_argument("--run-id", required=True, help="补跑批次 ID。")
    recovery_approve.add_argument("--plan-hash", required=True, help="preview 返回的计划哈希。")
    recovery_approve.add_argument("--approved-by", default=None, help="确认人标识。")

    recovery_control = recovery_commands.add_parser(
        "control", help="暂停、继续或取消补跑批次。"
    )
    recovery_control.add_argument("--run-id", required=True, help="补跑批次 ID。")
    recovery_control.add_argument(
        "--action", required=True, choices=["pause", "resume", "cancel"], help="控制动作。"
    )
    recovery_control.add_argument("--actor", default=None, help="操作人标识。")

    config = data_commands.add_parser("config", help="数据同步配置中心。")
    config_commands = config.add_subparsers(dest="subcommand", required=True)

    init = config_commands.add_parser("init", help="生成数据同步配置模板。")
    init.add_argument(
        "--preset",
        default="personal-ashare",
        help="配置预设：personal-ashare、personal-comprehensive、ashare-comprehensive、crypto-comprehensive、lightweight。",
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

    production = data_commands.add_parser("production", help="数据层生产化策略工具。")
    production_commands = production.add_subparsers(dest="subcommand", required=True)
    backfill = production_commands.add_parser(
        "backfill-plan",
        help="根据健康检查 JSON 生成缺口补采计划。",
    )
    backfill.add_argument(
        "--health-file",
        required=True,
        help="check_base_data_health 输出 JSON 文件。",
    )
    backfill.add_argument("--interval-seconds", type=int, default=300, help="导出调度任务间隔。")
    backfill.add_argument("--batch-size", type=int, default=200, help="导出调度任务批量大小。")
    merge = production_commands.add_parser("merge-universe", help="合并同市场多个候选池。")
    merge.add_argument("--target-universe-id", required=True, help="目标候选池 ID。")
    merge.add_argument("--name", required=True, help="目标候选池名称。")
    merge.add_argument("--source-universe-ids", required=True, help="逗号分隔的来源候选池 ID。")
    merge.add_argument(
        "--strategy-context",
        default="production_universe_merge",
        help="策略上下文。",
    )
    merge.add_argument(
        "--source-weights",
        default=None,
        help="可选 JSON，对来源候选池 ID 设置权重，例如 {\"u1\":2.0}。",
    )
    avoid = production_commands.add_parser(
        "rebuild-avoid-pool",
        help="根据资产状态和风险发现重建回避池。",
    )
    avoid.add_argument("--universe-id", required=True, help="回避池候选池 ID。")
    avoid.add_argument("--name", required=True, help="回避池名称。")
    avoid.add_argument(
        "--market",
        required=True,
        choices=["ashare", "crypto_spot", "crypto_future"],
    )
    avoid.add_argument("--strategy-context", default="avoid_pool", help="策略上下文。")

    strategy = data_commands.add_parser("strategy", help="评分策略权重管理。")
    strategy_commands = strategy.add_subparsers(dest="subcommand", required=True)
    strategy_commands.add_parser("init", help="写入默认评分策略。")
    strategy_list = strategy_commands.add_parser("list", help="列出评分策略。")
    strategy_list.add_argument(
        "--market",
        default=None,
        choices=["ashare", "crypto_spot", "crypto_future"],
        help="按市场过滤。",
    )
    strategy_list.add_argument(
        "--status",
        default=None,
        choices=["active", "draft", "archived"],
        help="按策略状态过滤。",
    )
    strategy_show = strategy_commands.add_parser("show", help="查看单个评分策略。")
    strategy_show.add_argument("strategy_id", help="评分策略 ID。")
    strategy_set = strategy_commands.add_parser("set", help="新增或更新评分策略。")
    strategy_set.add_argument("strategy_id", help="评分策略 ID。")
    strategy_set.add_argument(
        "--market",
        required=True,
        choices=["ashare", "crypto_spot", "crypto_future"],
        help="适用市场。",
    )
    strategy_set.add_argument("--name", required=True, help="策略中文名称。")
    strategy_set.add_argument("--description", default="", help="策略说明。")
    strategy_set.add_argument(
        "--group-weights",
        required=True,
        help='因子组权重 JSON，例如 {"technical":0.7,"fundamental":0.3}。',
    )
    strategy_set.add_argument(
        "--missing-penalty",
        default='{"per_missing_group":4,"per_partial_group":1.5}',
        help="缺失惩罚 JSON。",
    )
    strategy_set.add_argument(
        "--activate",
        action="store_true",
        help="写入后直接标记为 active；未指定时保存为 draft。",
    )
    strategy_validate = strategy_commands.add_parser(
        "validate", help="查看策略历史与前向准入状态。"
    )
    strategy_validate.add_argument("--strategy-id", required=True, help="策略 ID。")
    strategy_validate.add_argument(
        "--market",
        default="ashare",
        choices=["ashare", "crypto_spot", "crypto_future"],
        help="策略市场，默认 A 股。",
    )


def dispatch_data(args: argparse.Namespace) -> JsonDict:
    """执行数据同步配置命令。"""

    if args.command == "recovery":
        return dispatch_data_recovery(args)
    if args.command == "production":
        return dispatch_data_production(args)
    if args.command == "strategy":
        return dispatch_data_strategy(args)
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


def dispatch_data_production(args: argparse.Namespace) -> JsonDict:
    """执行数据层生产化策略命令。"""

    if args.subcommand == "backfill-plan":
        health_payload = json.loads(Path(args.health_file).read_text(encoding="utf-8-sig"))
        jobs = DataBackfillPlanner().build_backfill_jobs(health_summary=health_payload)
        return {
            "status": "ok",
            "data": {
                "jobs": [
                    job.to_scheduler_job(
                        interval_seconds=args.interval_seconds,
                        batch_size=args.batch_size,
                    )
                    for job in jobs
                ],
                "job_count": len(jobs),
            },
        }

    if args.subcommand in {"merge-universe", "rebuild-avoid-pool"}:
        session_factory = create_session_factory(args.database_url)
        with session_scope(session_factory) as session:
            service = ProductionUniverseService(session)
            if args.subcommand == "merge-universe":
                plans = service.merge_universes(
                    target_universe_id=args.target_universe_id,
                    name=args.name,
                    source_universe_ids=parse_market_arg(args.source_universe_ids),
                    source_weights=parse_optional_json_object(args.source_weights),
                    strategy_context=args.strategy_context,
                )
                return {
                    "status": "ok",
                    "data": {
                        "target_universe_id": args.target_universe_id,
                        "member_count": len(plans),
                        "members": [plan.to_repository_payload() for plan in plans],
                    },
                }
            plans = service.rebuild_avoid_pool(
                universe_id=args.universe_id,
                name=args.name,
                market=args.market,
                strategy_context=args.strategy_context,
            )
            return {
                "status": "ok",
                "data": {
                    "universe_id": args.universe_id,
                    "member_count": len(plans),
                    "members": [plan.to_repository_payload() for plan in plans],
                },
            }
    raise ValueError(f"未知 data production 命令：{args.subcommand}")


def dispatch_data_strategy(args: argparse.Namespace) -> JsonDict:
    """执行评分策略管理命令。"""

    session_factory = create_session_factory(args.database_url)
    with session_scope(session_factory) as session:
        if args.subcommand == "validate":
            state = StrategyObservationRepository(session).get_trial_state(args.strategy_id)
            if state is None:
                return {
                    "status": "ok",
                    "data": {
                        "strategy_id": args.strategy_id,
                        "market": args.market,
                        "strategy_state": "research",
                        "historical_evidence_id": None,
                        "matured_t20_count": 0,
                        "reason_codes": ["strategy_state_missing"],
                        "allow_new_buys": False,
                    },
                }
            state_name = str(getattr(state, "state", "research"))
            from finance_agent.research.validation_gate import StrategyValidationGate

            decision = StrategyValidationGate().evaluate_runtime(state, action="buy_ready")
            return {
                "status": "ok",
                "data": {
                    "strategy_id": args.strategy_id,
                    "market": args.market,
                    "strategy_state": state_name,
                    "historical_evidence_id": getattr(state, "historical_evidence_id", None),
                    "matured_t20_count": decision.metrics.get("t20_count", 0),
                    "reason_codes": list(decision.reason_codes),
                    "allow_new_buys": decision.allowed,
                },
            }
        repository = ScoringStrategyRepository(session)
        if args.subcommand == "init":
            seeded = repository.seed_defaults(default_scoring_strategy_seeds())
            return {
                "status": "ok",
                "data": {
                    "seeded_count": len(seeded),
                    "strategy_ids": [item.strategy_id for item in seeded],
                    "strategies": [serialize_scoring_strategy(item) for item in seeded],
                },
            }
        if args.subcommand == "list":
            strategies = repository.list_strategies(
                market=getattr(args, "market", None),
                status=getattr(args, "status", None),
            )
            return {
                "status": "ok",
                "data": {
                    "count": len(strategies),
                    "strategies": [serialize_scoring_strategy(item) for item in strategies],
                },
            }
        if args.subcommand == "show":
            strategy = repository.get_strategy(args.strategy_id)
            if strategy is None:
                raise ValueError(f"找不到评分策略：{args.strategy_id}")
            return {
                "status": "ok",
                "data": {
                    "strategy": serialize_scoring_strategy(strategy),
                },
            }
        if args.subcommand == "set":
            existing = repository.get_strategy(args.strategy_id)
            if existing is not None and existing.status != "draft":
                raise ValueError(
                    f"评分策略 {args.strategy_id} 当前状态为 {existing.status}，只能修改 draft 策略。"
                )
            payload = validate_scoring_strategy_payload(
                {
                    "strategy_id": args.strategy_id,
                    "market": args.market,
                    "name": args.name,
                    "description": args.description,
                    "group_weights": parse_optional_json_object(args.group_weights),
                    "missing_penalty": parse_optional_json_object(args.missing_penalty),
                    "status": "active" if args.activate else "draft",
                }
            )
            strategy = repository.upsert_strategy(**payload)
            return {
                "status": "ok",
                "data": {
                    "strategy": serialize_scoring_strategy(strategy),
                },
            }
    raise ValueError(f"未知 data strategy 命令：{args.subcommand}")


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


def parse_optional_json_object(value: str | None) -> JsonDict | None:
    """解析可选 JSON 对象。"""

    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("参数必须是 JSON 对象。")
    return parsed


def serialize_scoring_strategy(strategy: Any) -> JsonDict:
    """把评分策略 ORM/对象转换成可序列化结构。"""

    return {
        "strategy_id": strategy.strategy_id,
        "market": strategy.market,
        "name": strategy.name,
        "description": strategy.description,
        "group_weights": dict(strategy.group_weights),
        "missing_penalty": dict(strategy.missing_penalty),
        "status": strategy.status,
    }


def _recovery_session_module():
    """打开独立会话并构造补跑门面（CLI 无请求级依赖）。"""

    from contextlib import contextmanager

    from finance_agent.storage.db import create_session_factory
    from finance_agent.storage.db import session_scope as scope

    factory = create_session_factory()

    @contextmanager
    def ctx():
        with scope(factory) as session:
            from finance_agent.data_recovery.assembly import (
                build_default_recovery_module,
            )

            yield build_default_recovery_module(session)

    return ctx()


def dispatch_data_recovery(args: argparse.Namespace) -> JsonDict:
    """分发 `finance-agent data recovery ...` 子命令。"""

    subcommand = str(getattr(args, "subcommand", ""))
    with _recovery_session_module() as module:
        if subcommand == "preview":
            return module.preview(requested_by=getattr(args, "requested_by", None))
        if subcommand == "list":
            return {"runs": module.list_runs(limit=int(getattr(args, "limit", 20)))}
        if subcommand == "status":
            return module.get(str(args.run_id)).to_dict()
        if subcommand == "approve":
            view = module.approve(
                run_id=str(args.run_id),
                plan_hash=str(args.plan_hash),
                approved_by=getattr(args, "approved_by", None),
            )
            module.session.flush()
            return view.to_dict()
        if subcommand == "control":
            view = module.control(
                str(args.run_id),
                str(args.action),
                actor=getattr(args, "actor", None),
            )
            module.session.flush()
            return view.to_dict()
    raise ValueError(f"未知补跑子命令: {subcommand}")
