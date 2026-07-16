"""历史 A 股关键词新闻实体重验命令。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析参数；默认 dry-run，显式 ``--apply`` 才写库。"""

    parser = argparse.ArgumentParser(description="重验历史 A 股关键词新闻实体相关性")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="把重验结果合并到事件和关联证据 payload；默认仅输出 dry-run 摘要",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="显式执行只读重验；不传模式参数时也默认为 dry-run",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多扫描的事件数；默认扫描全部关键词新闻",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="JSONB 批量更新分块大小，默认 500",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="可选数据库连接；默认读取 FINANCE_AGENT_DATABASE_URL",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """执行历史重验并输出结构化 JSON 摘要。"""

    from finance_agent.data.news_entity_revalidation import (
        StockNewsEntityRevalidationService,
    )
    from finance_agent.storage.db import create_session_factory, session_scope
    from finance_agent.storage.repositories import EventRepository

    args = parse_args(argv)
    session_factory = create_session_factory(args.database_url)
    with session_scope(session_factory) as session:
        result = StockNewsEntityRevalidationService(EventRepository(session)).run(
            apply=bool(args.apply),
            limit=args.limit,
            chunk_size=args.chunk_size,
        )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
