import pandas as pd

from finance_agent.data.providers.akshare_p1_provider import AshareSectorProvider


def test_fetch_industry_names_falls_back_to_eastmoney_catalog(monkeypatch) -> None:
    """AKShare 行业目录断连时，应使用东财 curl 目录兜底。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_industry_name_em",
        lambda: (_ for _ in ()).throw(ConnectionError("remote closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_industry_names",
        lambda limit=None: pd.DataFrame(
            [
                {"板块名称": "银行", "板块代码": "BK0475"},
                {"板块名称": "证券", "板块代码": "BK0473"},
                {"板块名称": "保险", "板块代码": "BK0474"},
            ]
        ).head(limit),
    )

    result = AshareSectorProvider().fetch_industry_names(limit=2)

    assert result.status == "available"
    assert result.payload["names"] == ["银行", "证券"]
    assert result.payload["actual_source"] == "eastmoney:curl_cffi:stock_board_industry_name_em"
    assert result.payload["fallback_used"] is True
    assert result.payload["fallback_trace"] == [
        {"source": "akshare:stock_board_industry_name_em", "error_message": "remote closed"}
    ]


def test_fetch_industry_names_falls_back_to_ths_when_eastmoney_catalog_fails(
    monkeypatch,
) -> None:
    """AKShare 和东财行业目录都失败时，应继续使用同花顺目录兜底。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_industry_name_em",
        lambda: (_ for _ in ()).throw(ConnectionError("remote closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_industry_names",
        lambda limit=None: (_ for _ in ()).throw(RuntimeError("eastmoney closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_industry_name_ths",
        lambda: (_ for _ in ()).throw(RuntimeError("ak ths closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ths_curl.fetch_industry_names",
        lambda limit=None: pd.DataFrame(
            [
                {"板块名称": "银行", "板块代码": "881155"},
                {"板块名称": "证券", "板块代码": "881157"},
            ]
        ).head(limit),
    )

    result = AshareSectorProvider().fetch_industry_names(limit=2)

    assert result.status == "available"
    assert result.payload["names"] == ["银行", "证券"]
    assert result.payload["actual_source"] == "ths:curl_cffi:stock_board_industry_name"
    assert result.payload["fallback_used"] is True
    assert result.payload["fallback_trace"] == [
        {"source": "akshare:stock_board_industry_name_em", "error_message": "remote closed"},
        {
            "source": "eastmoney:curl_cffi:stock_board_industry_name_em",
            "error_message": "eastmoney closed",
        },
        {
            "source": "akshare:stock_board_industry_name_ths",
            "error_message": "ak ths closed",
        },
    ]


def test_fetch_industry_names_falls_back_to_akshare_ths_catalog(monkeypatch) -> None:
    """东财行业目录失败时，应优先使用 AKShare 同花顺目录。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_industry_name_em",
        lambda: (_ for _ in ()).throw(ConnectionError("remote closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_industry_names",
        lambda limit=None: (_ for _ in ()).throw(RuntimeError("eastmoney closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_industry_name_ths",
        lambda: pd.DataFrame(
            [
                {"name": "半导体", "code": "881121"},
                {"name": "白酒", "code": "881273"},
            ]
        ),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ths_curl.fetch_industry_names",
        lambda limit=None: (_ for _ in ()).throw(AssertionError("不应调用自定义 THS curl")),
    )

    result = AshareSectorProvider().fetch_industry_names(limit=1)

    assert result.status == "available"
    assert result.payload["names"] == ["半导体"]
    assert result.payload["actual_source"] == "akshare:stock_board_industry_name_ths"


def test_fetch_concept_names_falls_back_to_eastmoney_catalog(monkeypatch) -> None:
    """AKShare 概念目录断连时，应使用东财 curl 目录兜底。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_concept_name_em",
        lambda: (_ for _ in ()).throw(ConnectionError("remote closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_concept_names",
        lambda limit=None: pd.DataFrame(
            [
                {"板块名称": "融资融券", "板块代码": "BK0500"},
                {"板块名称": "人工智能", "板块代码": "BK0800"},
                {"板块名称": "机器人概念", "板块代码": "BK0801"},
            ]
        ).head(limit),
    )

    result = AshareSectorProvider().fetch_concept_names(limit=2)

    assert result.status == "available"
    assert result.payload["names"] == ["融资融券", "人工智能"]
    assert result.payload["actual_source"] == "eastmoney:curl_cffi:stock_board_concept_name_em"
    assert result.payload["fallback_used"] is True
    assert result.payload["fallback_trace"] == [
        {"source": "akshare:stock_board_concept_name_em", "error_message": "remote closed"}
    ]


def test_fetch_concept_names_falls_back_to_ths_when_eastmoney_catalog_fails(
    monkeypatch,
) -> None:
    """AKShare 和东财概念目录都失败时，应继续使用同花顺目录兜底。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_concept_name_em",
        lambda: (_ for _ in ()).throw(ConnectionError("remote closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_concept_names",
        lambda limit=None: (_ for _ in ()).throw(RuntimeError("eastmoney closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_concept_name_ths",
        lambda: (_ for _ in ()).throw(RuntimeError("ak ths closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ths_curl.fetch_concept_names",
        lambda limit=None: pd.DataFrame(
            [
                {"板块名称": "融资融券", "板块代码": "301390"},
                {"板块名称": "人工智能", "板块代码": "308002"},
            ]
        ).head(limit),
    )

    result = AshareSectorProvider().fetch_concept_names(limit=2)

    assert result.status == "available"
    assert result.payload["names"] == ["融资融券", "人工智能"]
    assert result.payload["actual_source"] == "ths:curl_cffi:stock_board_concept_name"
    assert result.payload["fallback_used"] is True
    assert result.payload["fallback_trace"] == [
        {"source": "akshare:stock_board_concept_name_em", "error_message": "remote closed"},
        {
            "source": "eastmoney:curl_cffi:stock_board_concept_name_em",
            "error_message": "eastmoney closed",
        },
        {
            "source": "akshare:stock_board_concept_name_ths",
            "error_message": "ak ths closed",
        },
    ]


def test_fetch_concept_names_falls_back_to_akshare_ths_catalog(monkeypatch) -> None:
    """东财概念目录失败时，应优先使用 AKShare 同花顺目录。"""

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_concept_name_em",
        lambda: (_ for _ in ()).throw(ConnectionError("remote closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.eastmoney_curl.fetch_concept_names",
        lambda limit=None: (_ for _ in ()).throw(RuntimeError("eastmoney closed")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_board_concept_name_ths",
        lambda: pd.DataFrame(
            [
                {"name": "AI PC", "code": "309121"},
                {"name": "人工智能", "code": "308002"},
            ]
        ),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ths_curl.fetch_concept_names",
        lambda limit=None: (_ for _ in ()).throw(AssertionError("不应调用自定义 THS curl")),
    )

    result = AshareSectorProvider().fetch_concept_names(limit=2)

    assert result.status == "available"
    assert result.payload["names"] == ["AI PC", "人工智能"]
    assert result.payload["actual_source"] == "akshare:stock_board_concept_name_ths"
