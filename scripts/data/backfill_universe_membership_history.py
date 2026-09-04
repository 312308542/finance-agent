"""从可审计 raw_records 回填候选池成员历史（默认仅预览）。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackfillReport:
    """回填结果摘要。"""

    status: str
    accepted_records: int
    rejected_records: int
    reason: str | None = None


def build_report(records: list[dict[str, Any]], *, minimum_snapshots: int = 120) -> BackfillReport:
    """仅接受带采集时间、来源和内容哈希且能证明有效日期的归档。"""

    accepted = 0
    rejected = 0
    for record in records:
        payload = record.get("response_payload") or {}
        has_date = payload.get("valid_from") or payload.get("effective_from") or record.get("as_of")
        if record.get("collected_at") and record.get("provider") and record.get("content_hash") and has_date:
            accepted += 1
        else:
            rejected += 1
    if accepted < minimum_snapshots:
        return BackfillReport("insufficient_data", accepted, rejected, "independent_snapshots_below_minimum")
    return BackfillReport("ready", accepted, rejected)


def main() -> int:
    parser = argparse.ArgumentParser(description="回填候选池成员历史")
    parser.add_argument("--dry-run", action="store_true", help="只输出可回填数量")
    parser.parse_args()
    print("status=insufficient_data accepted_records=0 rejected_records=0 reason=no_database_reader")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
