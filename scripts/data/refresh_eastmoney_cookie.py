"""刷新东方财富匿名访问 Cookie。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from finance_agent.data.providers.eastmoney_curl import (
    DEFAULT_EASTMONEY_COOKIE_FILE,
    EASTMONEY_COOKIE_FILE_ENV,
    refresh_eastmoney_cookie_file,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="刷新东方财富匿名 Cookie")
    parser.add_argument(
        "--output",
        default=os.getenv(EASTMONEY_COOKIE_FILE_ENV, str(DEFAULT_EASTMONEY_COOKIE_FILE)),
        help="Cookie secret 文件路径，默认 runtime/secrets/eastmoney_cookie.json",
    )
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口，便于手动检查")
    parser.add_argument("--timeout-ms", type=int, default=45_000, help="页面加载超时时间")
    args = parser.parse_args()

    payload = refresh_eastmoney_cookie_file(
        output_path=Path(args.output),
        headed=args.headed,
        timeout_ms=args.timeout_ms,
    )
    print(
        "东方财富 Cookie 已刷新 "
        f"path={args.output} cookie_count={payload['cookie_count']} "
        f"updated_at={payload['updated_at']}"
    )


if __name__ == "__main__":
    main()
