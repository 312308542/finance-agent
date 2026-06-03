"""临时脚本：抓取东方财富新闻详情页正文并输出为 UTF-8 BOM 文本。"""

from __future__ import annotations

import argparse
from pathlib import Path

from finance_agent.data.providers.eastmoney_article_fetcher import EastmoneyArticleFetcher


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="抓取东方财富新闻正文并写入 txt")
    parser.add_argument("--url", required=True, help="东方财富新闻详情页链接")
    parser.add_argument(
        "--output",
        required=True,
        help="输出 txt 路径；推荐放到 runtime/debug 目录",
    )
    parser.add_argument(
        "--min-text-length",
        type=int,
        default=120,
        help="正文最小长度阈值，低于该值会标记为 unavailable",
    )
    return parser.parse_args()


def main() -> None:
    """执行抓取并写入文本文件。"""

    args = parse_args()
    fetcher = EastmoneyArticleFetcher(min_text_length=args.min_text_length)
    result = fetcher.fetch(args.url)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"url: {result.url}",
        f"status: {result.status}",
        f"title: {result.title or ''}",
        f"text_length: {result.text_length}",
        f"html_length: {result.html_length or ''}",
        f"truncated: {result.truncated}",
        f"error_message: {result.error_message or ''}",
        "",
        "正文：",
        result.full_text or "",
        "",
    ]
    # Windows 下使用 utf-8-sig，记事本和部分终端能稳定识别中文编码。
    output_path.write_text("\n".join(lines), encoding="utf-8-sig", newline="\n")
    print(f"wrote={output_path}")
    print(f"status={result.status}")
    print(f"text_length={result.text_length}")


if __name__ == "__main__":
    main()
