"""备份并清理数据库中的 smoke/test 样例数据。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent.storage.db import create_engine_from_settings
from finance_agent.storage.smoke_cleanup import run_smoke_cleanup


def main() -> None:
    """执行 smoke 测试数据清理。"""

    args = parse_args()
    engine = create_engine_from_settings(args.database_url)
    result = run_smoke_cleanup(
        engine,
        backup_root=Path(args.backup_root),
        execute=args.execute,
    )
    printable = {
        "backup_dir": result.backup_dir,
        "dry_run": result.dry_run,
        "matched_total": result.matched_total,
        "deleted_total": result.deleted_total,
        "tables": [
            {
                "table": item.table,
                "matched": item.matched,
                "deleted": item.deleted,
                "backup_file": item.backup_file,
            }
            for item in result.tables
            if item.matched or item.deleted
        ],
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="清理数据库中的 smoke/test 样例数据")
    parser.add_argument("--database-url", default=None, help="数据库连接地址，默认读取环境变量")
    parser.add_argument(
        "--backup-root",
        default="runtime/backups",
        help="备份目录根路径，默认 runtime/backups",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正执行删除；不传时只备份和预览。",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
