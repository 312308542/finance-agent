import json
from collections.abc import Iterator

import pandas as pd
import pytest

from finance_agent.data.providers import sina_curl


@pytest.fixture(autouse=True)
def clear_sina_sector_catalog_cache() -> Iterator[None]:
    """隔离目录缓存，避免网络适配器测试相互污染。"""

    clear_cache = sina_curl._fetch_sector_catalog.cache_clear
    clear_cache()
    yield
    clear_cache()


def test_fetch_industry_names_parses_sina_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """新浪行业目录应保留板块名称、标签和源端公司家数。"""

    payload = {
        "hangye_ZI01": "hangye_ZI01,银行业,33,0,0,0,0,0,sh600000,0,0,0,浦发银行",
        "hangye_ZA01": "hangye_ZA01,农业,16,0,0,0,0,0,bj920403,0,0,0,康农种业",
    }
    monkeypatch.setattr(
        sina_curl,
        "_sina_get_text",
        lambda url, params=None: f"var sector_data = {json.dumps(payload, ensure_ascii=True)}",
    )
    result = sina_curl.fetch_industry_names()

    assert result.to_dict("records") == [
        {"板块名称": "银行业", "板块代码": "hangye_ZI01", "公司家数": 33},
        {"板块名称": "农业", "板块代码": "hangye_ZA01", "公司家数": 16},
    ]
    assert result.attrs["actual_source"] == (
        "sina:curl_cffi:stock_sector_spot:industry_combined"
    )


def test_fetch_industry_names_merges_legacy_sina_industry_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """行业目录应合并官方行业和新浪行业两套互补分类。"""

    official_payload = {
        "hangye_ZA01": "hangye_ZA01,农业,16,0,0,0,0,0,bj920403,0,0,0,康农种业"
    }
    legacy_payload = {
        "new_blhy": "new_blhy,玻璃行业,19,0,0,0,0,0,sh600529,0,0,0,山东药玻"
    }

    def fake_get_text(url: str, params: dict[str, str] | None = None) -> str:
        payload = legacy_payload if "newSinaHy" in url else official_payload
        return f"var sector_data = {json.dumps(payload, ensure_ascii=True)}"

    monkeypatch.setattr(sina_curl, "_sina_get_text", fake_get_text)
    result = sina_curl.fetch_industry_names()

    assert result["板块名称"].tolist() == ["农业", "玻璃行业"]
    assert result.attrs["actual_source"] == (
        "sina:curl_cffi:stock_sector_spot:industry_combined"
    )
    assert result.attrs["sources"] == [
        "sina:curl_cffi:stock_sector_spot:industry",
        "sina:curl_cffi:stock_sector_spot:sina_industry",
    ]


def test_fetch_concept_members_returns_complete_rows_in_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新浪概念成员应使用目录标签并校验完整返回数量。"""

    catalog = pd.DataFrame(
        [{"板块名称": "华为汽车", "板块代码": "gn_hwqc", "公司家数": 2}]
    )
    monkeypatch.setattr(sina_curl, "_fetch_sector_catalog", lambda board_type: catalog)
    monkeypatch.setattr(
        sina_curl,
        "_sina_get_text",
        lambda url, params=None: json.dumps(
            [
                {"code": "600006", "name": "东风股份"},
                {"code": "002786", "name": "银宝山新"},
            ],
            ensure_ascii=True,
        ),
    )

    result = sina_curl.fetch_concept_members("华为汽车")

    assert result[["代码", "名称"]].to_dict("records") == [
        {"代码": "600006", "名称": "东风股份"},
        {"代码": "002786", "名称": "银宝山新"},
    ]
    assert result.attrs == {
        "actual_source": "sina:curl_cffi:stock_sector_detail:concept",
        "source_coverage": "full",
        "board_code": "gn_hwqc",
        "expected_count": 2,
        "fetched_count": 2,
    }


def test_fetch_sector_members_rejects_partial_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源端成员数少于目录声明时不得把部分报文伪装为完整覆盖。"""

    catalog = pd.DataFrame(
        [{"板块名称": "银行业", "板块代码": "hangye_ZI01", "公司家数": 2}]
    )
    monkeypatch.setattr(sina_curl, "_fetch_sector_catalog", lambda board_type: catalog)
    monkeypatch.setattr(
        sina_curl,
        "_sina_get_text",
        lambda url, params=None: json.dumps(
            [{"code": "600000", "name": "浦发银行"}],
            ensure_ascii=True,
        ),
    )

    with pytest.raises(ValueError, match="目录声明 2，只返回 1"):
        sina_curl.fetch_industry_members("银行业")


def test_legacy_sina_industry_members_follow_source_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新浪行业超过单页上限时，应按目录数量拉取全部分页。"""

    catalog = pd.DataFrame(
        [{"板块名称": "电子器件", "板块代码": "new_dzqj", "公司家数": 3}]
    )
    calls: list[dict[str, str]] = []

    def fake_get_text(url: str, params: dict[str, str] | None = None) -> str:
        assert params is not None
        calls.append(dict(params))
        page = int(params["page"])
        rows = (
            [
                {"code": "600001", "name": "样本一"},
                {"code": "600002", "name": "样本二"},
            ]
            if page == 1
            else [{"code": "600003", "name": "样本三"}]
        )
        return json.dumps(rows, ensure_ascii=True)

    monkeypatch.setattr(sina_curl, "_fetch_sector_catalog", lambda board_type: catalog)
    monkeypatch.setattr(sina_curl, "_sina_get_text", fake_get_text)

    result = sina_curl.fetch_industry_members("电子器件")

    assert result["代码"].tolist() == ["600001", "600002", "600003"]
    assert [call["page"] for call in calls] == ["1", "2"]
    assert result.attrs["actual_source"] == (
        "sina:curl_cffi:stock_sector_detail:sina_industry"
    )
    assert result.attrs["expected_count"] == 3
    assert result.attrs["fetched_count"] == 3


def test_legacy_sina_industry_keeps_mismatched_payload_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新浪行业目录与明细口径冲突时应保留数据，但不得标记完整。"""

    catalog = pd.DataFrame(
        [{"板块名称": "其它行业", "板块代码": "new_qtxy", "公司家数": 3}]
    )

    def fake_get_text(url: str, params: dict[str, str] | None = None) -> str:
        assert params is not None
        if params["page"] == "1":
            return json.dumps(
                [
                    {"code": "600001", "name": "样本一"},
                    {"code": "600002", "name": "样本二"},
                ],
                ensure_ascii=True,
            )
        return "[]"

    monkeypatch.setattr(sina_curl, "_fetch_sector_catalog", lambda board_type: catalog)
    monkeypatch.setattr(sina_curl, "_sina_get_text", fake_get_text)

    result = sina_curl.fetch_industry_members("其它行业")

    assert result["代码"].tolist() == ["600001", "600002"]
    assert result.attrs["source_coverage"] == "partial"
    assert result.attrs["expected_count"] == 3
    assert result.attrs["fetched_count"] == 2
