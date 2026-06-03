from datetime import UTC, datetime

from finance_agent.data.providers.eastmoney_article_fetcher import (
    ArticleFetchResult,
    EastmoneyArticleFetcher,
    extract_article_text,
)


class _FakeResponse:
    def __init__(self, text: str, *, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


def test_extract_article_text_prefers_content_body() -> None:
    """优先从东方财富正文容器提取可清洗的完整新闻正文。"""

    html = """
    <html>
      <body>
        <div id="ContentBody">
          <p>第一段正文，解释公司正在推进 A+H 上市。</p>
          <p>第二段正文，说明收入增长但利润承压。</p>
          <script>window.__noise = true;</script>
        </div>
      </body>
    </html>
    """

    assert extract_article_text(html) == (
        "第一段正文，解释公司正在推进 A+H 上市。 "
        "第二段正文，说明收入增长但利润承压。"
    )


def test_fetcher_returns_available_article_payload() -> None:
    """抓取成功时返回正文、长度和抓取元数据。"""

    html = """
    <div id="ContentBody">
      <p>这是一段足够长的正文，用于模拟东方财富文章详情页。</p>
      <p>正文继续补充事件背景、财务影响和潜在风险。</p>
    </div>
    """

    def fake_get(url: str, **_kwargs: object) -> _FakeResponse:
        assert url == "http://finance.eastmoney.com/a/demo.html"
        return _FakeResponse(html)

    fetcher = EastmoneyArticleFetcher(http_get=fake_get, min_text_length=10)

    result = fetcher.fetch("http://finance.eastmoney.com/a/demo.html")

    assert result.status == "available"
    assert result.full_text is not None
    assert "财务影响" in result.full_text
    assert result.text_length == len(result.full_text)
    assert result.to_payload()["full_text"] == result.full_text


def test_article_fetch_result_error_payload() -> None:
    """失败结果也要以结构化 payload 保存，方便后续补偿重试。"""

    result = ArticleFetchResult(
        url="http://finance.eastmoney.com/a/missing.html",
        status="error",
        fetched_at=datetime(2026, 6, 3, tzinfo=UTC),
        error_message="timeout",
    )

    assert result.to_payload() == {
        "url": "http://finance.eastmoney.com/a/missing.html",
        "status": "error",
        "source": "eastmoney:article_page",
        "fetched_at": "2026-06-03T00:00:00+00:00",
        "title": None,
        "full_text": None,
        "text_length": 0,
        "html_length": None,
        "truncated": False,
        "error_message": "timeout",
    }
