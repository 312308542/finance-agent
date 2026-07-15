import pandas as pd

from finance_agent.data.providers import eastmoney_curl


def test_ths_fund_flow_uses_target_window_page_count(monkeypatch) -> None:
    """同花顺多日资金流应按目标周期分页，不能复用今日榜单页数。"""

    requested_urls: list[str] = []

    def fake_get_text(url: str, *, referer: str) -> str:
        requested_urls.append(url)
        if "/board/5/" not in url:
            return '<span class="page_info">1/1</span>'
        page = url.split("/page/", maxsplit=1)[1].split("/", maxsplit=1)[0]
        return f'<span class="page_info">{page}/3</span>'

    source_frame = pd.DataFrame(
        [
            {
                "序号": 1,
                "代码": "000001",
                "名称": "平安银行",
                "最新价": 10.0,
                "涨跌幅": "1%",
                "连续换手率": "2%",
                "主力净流入": "3万",
            }
        ]
    )
    monkeypatch.setattr(eastmoney_curl, "_ths_get_text", fake_get_text)
    monkeypatch.setattr(
        eastmoney_curl,
        "_read_ths_html_tables",
        lambda html: [source_frame.copy()],
    )

    result = eastmoney_curl._fetch_ths_fund_flow_rank("5日")

    assert len(result) == 3
    assert requested_urls == [
        "http://data.10jqka.com.cn/funds/ggzjl/board/5/field/zdf/order/desc/page/1/ajax/1/free/1/",
        "http://data.10jqka.com.cn/funds/ggzjl/board/5/field/zdf/order/desc/page/2/ajax/1/free/1/",
        "http://data.10jqka.com.cn/funds/ggzjl/board/5/field/zdf/order/desc/page/3/ajax/1/free/1/",
    ]


def test_ths_fund_flow_restores_numeric_stock_code_leading_zeros(monkeypatch) -> None:
    """同花顺表格把深市代码读成整数时，应在专用适配层恢复为六位代码。"""

    source_frame = pd.DataFrame(
        [
            [1, 1, "平安银行", 10.0, "1%", "2%", "3万"],
            [2, 2594, "比亚迪", 100.0, "2%", "3%", "4万"],
            [3, 600519, "贵州茅台", 1400.0, "3%", "4%", "5万"],
        ],
        columns=["序号", "代码", "名称", "最新价", "涨跌幅", "连续换手率", "主力净流入"],
    )
    monkeypatch.setattr(
        eastmoney_curl,
        "_ths_get_text",
        lambda url, referer: '<span class="page_info">1/1</span>',
    )
    monkeypatch.setattr(
        eastmoney_curl,
        "_read_ths_html_tables",
        lambda html: [source_frame.copy()],
    )

    result = eastmoney_curl._fetch_ths_fund_flow_rank("5日")

    assert result["代码"].tolist() == ["000001", "002594", "600519"]
