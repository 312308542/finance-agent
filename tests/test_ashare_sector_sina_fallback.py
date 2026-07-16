import pandas as pd
import pytest

from finance_agent.data.providers.akshare_p1_provider import AshareSectorProvider


def test_full_industry_catalog_merges_sina_supplement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量行业目录应合并新浪补充分类，并保持主源顺序和去重。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_industry_name_em",
        lambda: pd.DataFrame([{"板块名称": "银行"}, {"板块名称": "证券"}]),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.sina_curl.fetch_industry_names",
        lambda limit=None: _frame_with_source(
            [{"板块名称": "银行"}, {"板块名称": "银行业"}],
            "sina:curl_cffi:stock_sector_spot:industry",
        ),
    )

    result = AshareSectorProvider().fetch_industry_names()

    assert result.payload["names"] == ["银行", "证券", "银行业"]
    assert result.payload["actual_source"] == "akshare:stock_board_industry_name_em"
    assert result.payload["supplemental_sources"] == [
        "sina:curl_cffi:stock_sector_spot:industry"
    ]
    assert result.payload["supplemental_trace"] == []


def test_industry_members_use_complete_sina_before_ths_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """东财成员源失败后，应优先使用新浪完整成员而不是同花顺首屏。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.sina_curl.has_industry",
        lambda symbol: False,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_industry_cons_em",
        lambda symbol: (_ for _ in ()).throw(ConnectionError("ak eastmoney closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_industry_members",
        lambda symbol: (_ for _ in ()).throw(ConnectionError("curl eastmoney closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.sina_curl.fetch_industry_members",
        lambda symbol, limit=None: _member_frame(
            [{"代码": "600000", "名称": "浦发银行"}],
            source="sina:curl_cffi:stock_sector_detail:industry",
            coverage="full",
            board_code="hangye_ZI01",
        ),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ths_curl.fetch_industry_members",
        lambda symbol, limit=None: (_ for _ in ()).throw(
            AssertionError("新浪完整成员可用时不应调用同花顺首屏")
        ),
    )

    result = AshareSectorProvider().fetch_industry_members(industry_name="银行业")

    assert result.status == "available"
    assert result.payload["row_count"] == 1
    assert result.payload["actual_source"] == "sina:curl_cffi:stock_sector_detail:industry"
    assert result.payload["source_coverage"] == "full"
    assert result.payload["source_board_code"] == "hangye_ZI01"
    assert [item.symbol for item in result.seeds] == ["600000"]


def test_sina_industry_catalog_member_routes_without_waiting_for_eastmoney(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新浪目录自有行业应直接路由，避免每个任务先等待东财超时。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.sina_curl.has_industry",
        lambda symbol: symbol == "农业",
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.sina_curl.fetch_industry_members",
        lambda symbol, limit=None: _member_frame(
            [{"代码": "600108", "名称": "亚盛集团"}],
            source="sina:curl_cffi:stock_sector_detail:industry",
            coverage="full",
            board_code="hangye_ZA01",
        ),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_industry_cons_em",
        lambda symbol: (_ for _ in ()).throw(
            AssertionError("新浪目录自有板块不应先请求 AKShare 东财")
        ),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_industry_members",
        lambda symbol: (_ for _ in ()).throw(
            AssertionError("新浪目录自有板块不应先请求东财 curl")
        ),
    )

    result = AshareSectorProvider().fetch_industry_members(industry_name="农业")

    assert result.status == "available"
    assert result.payload["source_routed"] is True
    assert result.payload["actual_source"] == "sina:curl_cffi:stock_sector_detail:industry"
    assert [item.symbol for item in result.seeds] == ["600108"]


def test_concept_members_keep_ths_as_last_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新浪也失败时，应保留同花顺首屏作为最后降级路径。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.sina_curl.has_concept",
        lambda symbol: False,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_concept_cons_em",
        lambda symbol: (_ for _ in ()).throw(ConnectionError("ak eastmoney closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_concept_members",
        lambda symbol: (_ for _ in ()).throw(ConnectionError("curl eastmoney closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.sina_curl.fetch_concept_members",
        lambda symbol, limit=None: (_ for _ in ()).throw(KeyError("新浪无同名概念")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ths_curl.fetch_concept_members",
        lambda symbol, limit=None: _member_frame(
            [{"代码": "000001", "名称": "平安银行"}],
            source="ths:curl_cffi:stock_board_concept_detail_first_page",
            coverage="first_page",
            board_code="302035",
        ),
    )

    result = AshareSectorProvider().fetch_concept_members(concept_name="人工智能")

    assert result.status == "available"
    assert result.payload["actual_source"] == (
        "ths:curl_cffi:stock_board_concept_detail_first_page"
    )
    assert result.payload["source_coverage"] == "first_page"
    assert result.payload["fallback_trace"][-1] == {
        "source": "sina:curl_cffi:stock_sector_detail:concept",
        "error_message": "'新浪无同名概念'",
    }


def _frame_with_source(rows: list[dict[str, str]], source: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame.attrs["actual_source"] = source
    return frame


def _member_frame(
    rows: list[dict[str, str]],
    *,
    source: str,
    coverage: str,
    board_code: str,
) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame.attrs.update(
        {
            "actual_source": source,
            "source_coverage": coverage,
            "board_code": board_code,
        }
    )
    return frame
