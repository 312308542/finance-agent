from datetime import UTC, datetime
from types import SimpleNamespace

from finance_agent.data.collectors import AshareP1Collector
from finance_agent.data.models import EventRecordData, EventRecordsResult, EvidenceData
from finance_agent.data.providers.eastmoney_article_fetcher import ArticleFetchResult


class _FakeEventProvider:
    def fetch_stock_news(self, *, symbol: str, limit: int | None = None) -> EventRecordsResult:
        collected_at = datetime(2026, 6, 3, tzinfo=UTC)
        return EventRecordsResult(
            provider_name="akshare",
            status="available",
            collected_at=collected_at,
            events=[
                EventRecordData(
                    event_id="event:news:000001:1",
                    asset_id=f"ashare:{symbol}",
                    symbol=symbol,
                    market="ashare",
                    event_type="news",
                    title="平安银行新闻",
                    source="akshare:stock_news_em",
                    collected_at=collected_at,
                    summary="接口摘要",
                    url="http://finance.eastmoney.com/a/demo.html",
                )
            ],
            evidence=[
                EvidenceData(
                    evidence_id="evidence:news:000001:1",
                    evidence_type="news",
                    asset_id=f"ashare:{symbol}",
                    source="akshare:stock_news_em",
                    title="平安银行新闻",
                    reliability="medium",
                    collected_at=collected_at,
                    summary="接口摘要",
                    data_ref="event:news:000001:1",
                    url="http://finance.eastmoney.com/a/demo.html",
                )
            ],
            payload={"endpoint": "stock_news_em", "symbol": symbol},
        )


class _FakeRawRecords:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def insert_raw_record(self, **_kwargs: object) -> SimpleNamespace:
        self.calls.append(_kwargs)
        return SimpleNamespace(raw_record_id="raw:stock_news:000001")


class _FakeAssets:
    def __init__(self) -> None:
        self.ensured: list[dict[str, object]] = []

    def ensure_asset(self, **kwargs: object) -> None:
        self.ensured.append(kwargs)

    def upsert_asset_profile(self, **_kwargs: object) -> None:
        raise AssertionError("新闻事件源不应该写入 asset_profiles")


class _FakeEvents:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.evidence: list[dict[str, object]] = []
        self.event_article_updates: list[dict[str, object]] = []
        self.evidence_article_updates: list[dict[str, object]] = []
        self.event_article_batches: list[list[dict[str, object]]] = []
        self.evidence_article_batches: list[list[dict[str, object]]] = []

    def upsert_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)

    def upsert_evidence(self, **kwargs: object) -> None:
        self.evidence.append(kwargs)

    def update_event_article_payload(self, *, event_id: str, article_payload: dict) -> None:
        raise AssertionError("正文回填不应逐条 update event_records")

    def update_evidence_article_payloads_by_event(
        self,
        *,
        event_id: str,
        article_payload: dict,
    ) -> int:
        raise AssertionError("正文回填不应逐条 update evidence")

    def update_event_article_payloads(self, rows: list[dict[str, object]]) -> int:
        self.event_article_batches.append(rows)
        self.event_article_updates.extend(rows)
        return len(rows)

    def update_evidence_article_payloads_by_events(
        self,
        rows: list[dict[str, object]],
    ) -> int:
        self.evidence_article_batches.append(rows)
        self.evidence_article_updates.extend(rows)
        return len(rows)


class _FakeArticleFetcher:
    def __init__(self, result: ArticleFetchResult | Exception) -> None:
        self.result = result
        self.urls: list[str] = []

    def fetch(self, url: str) -> ArticleFetchResult:
        self.urls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _build_collector(article_fetcher: object | None = None) -> AshareP1Collector:
    collector = AshareP1Collector.__new__(AshareP1Collector)
    collector.event_provider = _FakeEventProvider()
    collector.raw_records = _FakeRawRecords()
    collector.assets = _FakeAssets()
    collector.events = _FakeEvents()
    collector.article_fetcher = article_fetcher
    return collector


def test_collect_stock_news_does_not_write_asset_profile() -> None:
    """新闻采集只写事件和证据，不把新闻来源写入资产慢变资料。"""

    collector = _build_collector(article_fetcher=None)

    result = AshareP1Collector.collect_stock_news(
        collector,
        symbol="000001",
        asset_name="平安银行",
    )

    assert result.raw_record_id == "raw:stock_news:000001"
    assert collector.events.events[0]["asset_id"] == "ashare:000001"
    assert collector.events.evidence[0]["asset_id"] == "ashare:000001"


def test_collect_stock_news_enriches_article_full_text() -> None:
    """二次抓取成功时，事件和证据 payload 都带完整正文。"""

    fetched_at = datetime(2026, 6, 3, 1, 2, 3, tzinfo=UTC)
    article = ArticleFetchResult(
        url="http://finance.eastmoney.com/a/demo.html",
        status="available",
        fetched_at=fetched_at,
        title="平安银行新闻",
        full_text="完整正文，包含比 AKShare 摘要更长的事件背景和影响分析。",
        text_length=30,
        html_length=1024,
    )
    fetcher = _FakeArticleFetcher(article)
    collector = _build_collector(article_fetcher=fetcher)

    AshareP1Collector.collect_stock_news(collector, symbol="000001", asset_name="平安银行")

    assert fetcher.urls == ["http://finance.eastmoney.com/a/demo.html"]
    event_payload = collector.events.events[0]["payload"]
    evidence_payload = collector.events.evidence[0]["payload"]
    assert event_payload["article"]["status"] == "available"
    assert event_payload["article"]["full_text"] == article.full_text
    assert event_payload["article"]["source_excerpt"] == "接口摘要"
    assert evidence_payload["article"]["full_text"] == article.full_text
    raw_payload = collector.raw_records.calls[0]["response_payload"]
    assert raw_payload["article_fetch"]["available"] == 1
    assert raw_payload["events"][0]["payload"]["article"]["full_text"] == article.full_text


def test_collect_stock_news_can_skip_inline_article_fetch() -> None:
    """调度任务可跳过同步原文抓取，让新闻列表先快速入库。"""

    fetcher = _FakeArticleFetcher(RuntimeError("should not fetch"))
    collector = _build_collector(article_fetcher=fetcher)

    AshareP1Collector.collect_stock_news(
        collector,
        symbol="000001",
        asset_name="平安银行",
        enrich_articles=False,
    )

    assert fetcher.urls == []
    event_payload = collector.events.events[0]["payload"]
    raw_payload = collector.raw_records.calls[0]["response_payload"]
    assert "article" not in event_payload
    assert "article_fetch" not in raw_payload


def test_collect_stock_news_records_article_fetch_error() -> None:
    """二次抓取失败时，不影响新闻事件入库，并记录失败原因。"""

    collector = _build_collector(article_fetcher=_FakeArticleFetcher(RuntimeError("timeout")))

    AshareP1Collector.collect_stock_news(collector, symbol="000001", asset_name="平安银行")

    event_payload = collector.events.events[0]["payload"]
    evidence_payload = collector.events.evidence[0]["payload"]
    assert event_payload["article"]["status"] == "error"
    assert event_payload["article"]["error_message"] == "timeout"
    assert evidence_payload["article"]["status"] == "error"


def test_enrich_existing_stock_news_article_updates_event_and_evidence_payload() -> None:
    """独立正文补抓任务应回填已入库事件和证据的 article payload。"""

    fetched_at = datetime(2026, 6, 3, 1, 2, 3, tzinfo=UTC)
    article = ArticleFetchResult(
        url="http://finance.eastmoney.com/a/demo.html",
        status="available",
        fetched_at=fetched_at,
        title="平安银行新闻",
        full_text="完整正文，包含比 AKShare 摘要更长的事件背景和影响分析。",
        text_length=30,
        html_length=1024,
    )
    collector = _build_collector(article_fetcher=_FakeArticleFetcher(article))

    result = AshareP1Collector.enrich_existing_stock_news_article(
        collector,
        event_id="event:news:000001:1",
        url="http://finance.eastmoney.com/a/demo.html",
        asset_id="ashare:000001",
        symbol="000001",
        title="平安银行新闻",
        source_excerpt="接口摘要",
    )

    assert result.result.status == "available"
    article_payload = collector.events.event_article_updates[0]["article_payload"]
    assert article_payload["full_text"] == article.full_text
    assert article_payload["source_excerpt"] == "接口摘要"
    assert collector.events.evidence_article_updates == [
        {
            "event_id": "event:news:000001:1",
            "article_payload": article_payload,
        }
    ]
    raw_payload = collector.raw_records.calls[0]["response_payload"]
    assert raw_payload["article_fetch"]["available"] == 1
