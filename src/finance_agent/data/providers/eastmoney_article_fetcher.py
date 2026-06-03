"""东方财富新闻详情页正文抓取器。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from typing import Any, Protocol

import requests

JsonDict = dict[str, Any]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
)


class _HttpResponse(Protocol):
    """正文抓取所需的最小 HTTP 响应协议。"""

    status_code: int
    text: str
    encoding: str | None
    apparent_encoding: str | None


HttpGet = Callable[..., _HttpResponse]


@dataclass(frozen=True)
class ArticleFetchResult:
    """新闻详情页正文抓取结果。"""

    url: str
    status: str
    fetched_at: datetime
    source: str = "eastmoney:article_page"
    title: str | None = None
    full_text: str | None = None
    text_length: int = 0
    html_length: int | None = None
    truncated: bool = False
    error_message: str | None = None

    def to_payload(self) -> JsonDict:
        """转换为可写入 JSONB 的结构化 payload。"""

        return {
            "url": self.url,
            "status": self.status,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
            "title": self.title,
            "full_text": self.full_text,
            "text_length": self.text_length,
            "html_length": self.html_length,
            "truncated": self.truncated,
            "error_message": self.error_message,
        }


class EastmoneyArticleFetcher:
    """根据东方财富新闻链接补抓完整正文。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 12,
        min_text_length: int = 120,
        max_text_chars: int = 30000,
        http_get: HttpGet | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_text_length = min_text_length
        self.max_text_chars = max_text_chars
        self.http_get = http_get or requests.get

    def fetch(self, url: str) -> ArticleFetchResult:
        """抓取新闻详情页，并尽量抽取正文文本。"""

        fetched_at = datetime.now(tz=UTC)
        try:
            response = self.http_get(
                url,
                timeout=self.timeout_seconds,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
        except Exception as exc:
            return ArticleFetchResult(
                url=url,
                status="error",
                fetched_at=fetched_at,
                error_message=str(exc),
            )

        html = _response_text(response)
        if response.status_code >= 400:
            return ArticleFetchResult(
                url=url,
                status="error",
                fetched_at=fetched_at,
                html_length=len(html),
                error_message=f"http_status={response.status_code}",
            )

        full_text = extract_article_text(html)
        if len(full_text) < self.min_text_length:
            return ArticleFetchResult(
                url=url,
                status="unavailable",
                fetched_at=fetched_at,
                html_length=len(html),
                full_text=full_text or None,
                text_length=len(full_text),
                error_message="正文长度低于阈值",
            )

        truncated = len(full_text) > self.max_text_chars
        if truncated:
            full_text = full_text[: self.max_text_chars].rstrip()

        return ArticleFetchResult(
            url=url,
            status="available",
            fetched_at=fetched_at,
            title=extract_title(html),
            full_text=full_text,
            text_length=len(full_text),
            html_length=len(html),
            truncated=truncated,
        )


def _response_text(response: _HttpResponse) -> str:
    """按响应声明或 apparent_encoding 解码正文。"""

    if getattr(response, "apparent_encoding", None):
        response.encoding = response.apparent_encoding
    return response.text or ""


def extract_title(html: str) -> str | None:
    """从详情页 HTML 中提取标题。"""

    for pattern in (
        r"<h1[^>]*>(.*?)</h1>",
        r"<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, html, flags=re.I | re.S)
        if not match:
            continue
        title = _clean_html_fragment(match.group(1))
        if title:
            return title
    return None


def extract_article_text(html: str) -> str:
    """从东方财富新闻页 HTML 中抽取正文文本。"""

    candidates: list[str] = []
    for pattern in (
        r"<div[^>]+id=[\"']ContentBody[\"'][^>]*>(.*?)</div>",
        r"<div[^>]+class=[\"'][^\"']*(?:newsContent|article|Article|content|Content)[^\"']*[\"'][^>]*>(.*?)</div>",
        r"<article[^>]*>(.*?)</article>",
    ):
        for match in re.finditer(pattern, html, flags=re.I | re.S):
            text = _clean_html_fragment(match.group(1))
            if text:
                candidates.append(text)

    if candidates:
        return max(candidates, key=len)
    return _clean_html_fragment(html)


def _clean_html_fragment(fragment: str) -> str:
    """清理 HTML 标签、脚本和多余空白。"""

    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", fragment, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*", "\n", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " ".join(lines).strip()
