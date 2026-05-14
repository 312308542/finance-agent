"""AKShare A 股能力注册表。

这个文件不直接抓数据，只负责描述“AKShare 的哪些接口应该被系统利用”。
Provider、定时任务、健康检查和后续因子服务都从这里读取接口用途，
避免把 AKShare 能力散落在多个脚本里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AkshareCapability:
    """单个 AKShare 接口在推荐系统里的用途定义。"""

    name: str
    provider_class: str
    data_domain: str
    priority: str
    recommendation_stages: tuple[str, ...]
    storage_targets: tuple[str, ...]
    factor_groups: tuple[str, ...] = ()
    description: str = ""
    sample_params: JsonDict = field(default_factory=dict)
    fallback_names: tuple[str, ...] = ()
    enabled_in_mvp: bool = False
    notes: str = ""


AKSHARE_CAPABILITIES: tuple[AkshareCapability, ...] = (
    # P0：资产、行情和交易状态。没有这些，后续推荐链路无法闭合。
    AkshareCapability(
        name="stock_zh_a_spot_em",
        provider_class="AshareUniverseProvider",
        data_domain="universe",
        priority="P0",
        recommendation_stages=("候选池来源", "数据采集", "初筛规则"),
        storage_targets=("assets", "asset_universes", "asset_universe_members", "raw_records"),
        factor_groups=("liquidity", "valuation"),
        description="东方财富 A 股实时行情，全 A 种子池和实时快照主源。",
        fallback_names=("stock_zh_a_spot_tx",),
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_zh_a_spot_tx",
        provider_class="AshareUniverseProvider",
        data_domain="universe",
        priority="P0",
        recommendation_stages=("候选池来源", "数据采集", "初筛规则"),
        storage_targets=("assets", "asset_universes", "asset_universe_members", "raw_records"),
        factor_groups=("liquidity", "valuation", "flow"),
        description="腾讯 A 股实时行情，作为东方财富实时行情 fallback。",
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_zh_a_hist",
        provider_class="AshareMarketDataProvider",
        data_domain="market_bar",
        priority="P0",
        recommendation_stages=("数据采集", "因子计算", "回测验证"),
        storage_targets=("market_bars", "raw_records"),
        factor_groups=("technical", "liquidity", "volatility"),
        description="东方财富 A 股日线/周线/月线，技术因子和回测主源。",
        sample_params={
            "symbol": "000001",
            "period": "daily",
            "start_date": "20260501",
            "end_date": "20260514",
            "adjust": "qfq",
        },
        fallback_names=("stock_zh_a_hist_tx", "eastmoney_kline_curl_cffi"),
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_zh_a_hist_tx",
        provider_class="AshareMarketDataProvider",
        data_domain="market_bar",
        priority="P0",
        recommendation_stages=("数据采集", "因子计算", "回测验证"),
        storage_targets=("market_bars", "raw_records"),
        factor_groups=("technical", "liquidity", "volatility"),
        description="腾讯 A 股日线行情，作为东方财富历史行情 fallback。",
        sample_params={
            "symbol": "sz000001",
            "start_date": "20260501",
            "end_date": "20260514",
            "adjust": "qfq",
        },
        enabled_in_mvp=True,
        notes="腾讯接口只有日线 OHLC 和成交额，缺少成交量、涨跌幅、换手率等字段。",
    ),
    AkshareCapability(
        name="stock_zh_a_stop_em",
        provider_class="AshareRiskProvider",
        data_domain="risk",
        priority="P0",
        recommendation_stages=("初筛规则", "风险反驳"),
        storage_targets=("event_records", "risk_findings", "raw_records"),
        factor_groups=("risk",),
        description="停复牌信息，用于剔除不可交易标的。",
        enabled_in_mvp=True,
    ),
    # P1：让推荐解释具备行业、资金、事件和公告证据。
    AkshareCapability(
        name="stock_board_industry_name_em",
        provider_class="AshareSectorProvider",
        data_domain="sector",
        priority="P1",
        recommendation_stages=("候选池来源", "数据采集", "因子计算"),
        storage_targets=("asset_universes", "asset_universe_members", "raw_records"),
        factor_groups=("sector", "momentum"),
        description="东方财富行业板块列表，用于行业种子池和行业轮动。",
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_board_industry_cons_em",
        provider_class="AshareSectorProvider",
        data_domain="sector",
        priority="P1",
        recommendation_stages=("候选池来源", "数据采集", "因子计算"),
        storage_targets=("asset_universes", "asset_universe_members", "raw_records"),
        factor_groups=("sector",),
        description="东方财富行业板块成分，用于行业候选池成员。",
        sample_params={"symbol": "银行"},
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_board_concept_name_em",
        provider_class="AshareSectorProvider",
        data_domain="theme",
        priority="P1",
        recommendation_stages=("候选池来源", "数据采集", "因子计算"),
        storage_targets=("asset_universes", "asset_universe_members", "raw_records"),
        factor_groups=("theme", "sentiment"),
        description="东方财富概念板块列表，用于主题种子池。",
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_board_concept_cons_em",
        provider_class="AshareSectorProvider",
        data_domain="theme",
        priority="P1",
        recommendation_stages=("候选池来源", "数据采集", "因子计算"),
        storage_targets=("asset_universes", "asset_universe_members", "raw_records"),
        factor_groups=("theme",),
        description="东方财富概念板块成分，用于主题候选池成员。",
        sample_params={"symbol": "人工智能"},
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_individual_fund_flow",
        provider_class="AshareCapitalFlowProvider",
        data_domain="capital_flow",
        priority="P1",
        recommendation_stages=("数据采集", "因子计算", "Agent 分析"),
        storage_targets=("capital_flow_snapshots", "evidence", "raw_records"),
        factor_groups=("flow",),
        description="个股主力资金流，用于资金流强度、连续流入和资金背离。",
        sample_params={"stock": "000001", "market": "sz"},
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_individual_fund_flow_rank",
        provider_class="AshareCapitalFlowProvider",
        data_domain="capital_flow",
        priority="P1",
        recommendation_stages=("候选池来源", "因子计算", "Agent 分析"),
        storage_targets=("asset_universes", "capital_flow_snapshots", "raw_records"),
        factor_groups=("flow",),
        description="个股资金流排名，用于资金流榜单种子源。",
        sample_params={"indicator": "今日"},
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_main_fund_flow",
        provider_class="AshareCapitalFlowProvider",
        data_domain="capital_flow",
        priority="P1",
        recommendation_stages=("候选池来源", "因子计算", "Agent 分析"),
        storage_targets=("capital_flow_snapshots", "raw_records"),
        factor_groups=("flow", "sector"),
        description="主力资金流排名，用于资金确认和行业资金强弱。",
        sample_params={"symbol": "全部股票"},
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_hsgt_hist_em",
        provider_class="AshareCapitalFlowProvider",
        data_domain="northbound_flow",
        priority="P1",
        recommendation_stages=("数据采集", "因子计算", "风险反驳"),
        storage_targets=("capital_flow_snapshots", "raw_records"),
        factor_groups=("flow", "market_breadth"),
        description="沪深港通历史资金流，作为北向资金确认信号。",
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_hsgt_individual_em",
        provider_class="AshareCapitalFlowProvider",
        data_domain="northbound_flow",
        priority="P1",
        recommendation_stages=("数据采集", "因子计算", "Agent 分析"),
        storage_targets=("capital_flow_snapshots", "evidence", "raw_records"),
        factor_groups=("flow",),
        description="沪深港通个股资金，用于外资偏好解释。",
        sample_params={"symbol": "北向资金"},
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_news_em",
        provider_class="AshareEventProvider",
        data_domain="event",
        priority="P1",
        recommendation_stages=("数据采集", "因子计算", "Agent 分析"),
        storage_targets=("event_records", "evidence", "raw_records"),
        factor_groups=("event",),
        description="个股新闻，用于事件证据和中文解释。",
        sample_params={"symbol": "000001"},
        enabled_in_mvp=True,
    ),
    AkshareCapability(
        name="stock_notice_report",
        provider_class="AshareEventProvider",
        data_domain="event",
        priority="P1",
        recommendation_stages=("数据采集", "初筛规则", "风险反驳"),
        storage_targets=("event_records", "evidence", "risk_findings", "raw_records"),
        factor_groups=("event", "risk"),
        description="公告和披露，用于重大事项、财报披露和风险反驳。",
        enabled_in_mvp=True,
    ),
    # P2：提升基本面、估值、风险和短线情绪质量。
    AkshareCapability(
        name="stock_yjbb_em",
        provider_class="AshareFundamentalProvider",
        data_domain="fundamental",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "Agent 分析"),
        storage_targets=("fundamental_snapshots", "raw_records"),
        factor_groups=("fundamental", "growth"),
        description="业绩报表，用于成长性和盈利变化。",
        sample_params={"date": "20260331"},
    ),
    AkshareCapability(
        name="stock_yjkb_em",
        provider_class="AshareFundamentalProvider",
        data_domain="fundamental",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "Agent 分析"),
        storage_targets=("fundamental_snapshots", "event_records", "raw_records"),
        factor_groups=("fundamental", "event"),
        description="业绩快报，用于业绩变化和事件新鲜度。",
        sample_params={"date": "20260331"},
    ),
    AkshareCapability(
        name="stock_yjyg_em",
        provider_class="AshareFundamentalProvider",
        data_domain="fundamental",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "风险反驳"),
        storage_targets=("fundamental_snapshots", "event_records", "raw_records"),
        factor_groups=("fundamental", "event", "risk"),
        description="业绩预告，用于预期变化和业绩风险。",
        sample_params={"date": "20260331"},
    ),
    AkshareCapability(
        name="stock_financial_analysis_indicator_em",
        provider_class="AshareFundamentalProvider",
        data_domain="fundamental",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "Agent 分析"),
        storage_targets=("fundamental_snapshots", "raw_records"),
        factor_groups=("fundamental", "quality"),
        description="东方财富主要财务指标，用于 ROE、利润率、负债率和现金流质量。",
        sample_params={"symbol": "000001"},
    ),
    AkshareCapability(
        name="stock_profit_forecast_em",
        provider_class="AshareFundamentalProvider",
        data_domain="fundamental",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "Agent 分析"),
        storage_targets=("fundamental_snapshots", "evidence", "raw_records"),
        factor_groups=("fundamental", "expectation"),
        description="盈利预测，用于一致预期和预期差。",
    ),
    AkshareCapability(
        name="stock_value_em",
        provider_class="AshareValuationProvider",
        data_domain="valuation",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "风险反驳"),
        storage_targets=("fundamental_snapshots", "risk_findings", "raw_records"),
        factor_groups=("valuation", "risk"),
        description="个股估值，用于 PE/PB/PS/股息率等估值判断。",
    ),
    AkshareCapability(
        name="stock_a_gxl_lg",
        provider_class="AshareValuationProvider",
        data_domain="valuation",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "Agent 分析"),
        storage_targets=("fundamental_snapshots", "raw_records"),
        factor_groups=("valuation", "dividend"),
        description="A 股股息率，用于红利因子和价值解释。",
    ),
    AkshareCapability(
        name="stock_hot_rank_em",
        provider_class="AshareSentimentProvider",
        data_domain="sentiment",
        priority="P2",
        recommendation_stages=("候选池来源", "因子计算", "风险反驳"),
        storage_targets=("asset_universes", "event_records", "raw_records"),
        factor_groups=("sentiment", "risk"),
        description="东方财富人气榜，用于热度种子源和短线拥挤度。",
    ),
    AkshareCapability(
        name="stock_zt_pool_em",
        provider_class="AshareSentimentProvider",
        data_domain="sentiment",
        priority="P2",
        recommendation_stages=("候选池来源", "因子计算", "风险反驳"),
        storage_targets=("asset_universes", "event_records", "risk_findings", "raw_records"),
        factor_groups=("sentiment", "risk"),
        description="涨停股池，用于强势种子源和情绪过热风险。",
        sample_params={"date": "20260514"},
    ),
    AkshareCapability(
        name="stock_lhb_detail_em",
        provider_class="AshareRiskProvider",
        data_domain="risk",
        priority="P2",
        recommendation_stages=("数据采集", "风险反驳", "Agent 分析"),
        storage_targets=("risk_findings", "evidence", "raw_records"),
        factor_groups=("risk", "sentiment"),
        description="龙虎榜明细，用于游资/机构活跃和短线交易风险。",
        sample_params={"start_date": "20260501", "end_date": "20260514"},
    ),
    AkshareCapability(
        name="stock_dzjy_mrmx",
        provider_class="AshareRiskProvider",
        data_domain="risk",
        priority="P2",
        recommendation_stages=("数据采集", "风险反驳", "Agent 分析"),
        storage_targets=("risk_findings", "evidence", "raw_records"),
        factor_groups=("risk",),
        description="大宗交易每日明细，用于折溢价异常和机构交易解释。",
        sample_params={"symbol": "A股", "start_date": "20260501", "end_date": "20260514"},
    ),
    AkshareCapability(
        name="stock_margin_sse",
        provider_class="AshareRiskProvider",
        data_domain="risk",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "风险反驳"),
        storage_targets=("risk_findings", "raw_records"),
        factor_groups=("risk", "sentiment"),
        description="上交所融资融券，用于杠杆情绪和拥挤风险。",
    ),
    AkshareCapability(
        name="stock_margin_szse",
        provider_class="AshareRiskProvider",
        data_domain="risk",
        priority="P2",
        recommendation_stages=("数据采集", "因子计算", "风险反驳"),
        storage_targets=("risk_findings", "raw_records"),
        factor_groups=("risk", "sentiment"),
        description="深交所融资融券，用于杠杆情绪和拥挤风险。",
    ),
    # P3：提升长期报告质量和市场环境判断。
    AkshareCapability(
        name="stock_institute_recommend",
        provider_class="AshareEventProvider",
        data_domain="research",
        priority="P3",
        recommendation_stages=("数据采集", "Agent 分析"),
        storage_targets=("evidence", "event_records", "raw_records"),
        factor_groups=("event", "expectation"),
        description="机构推荐，用于补充研报和一致预期证据。",
    ),
    AkshareCapability(
        name="stock_analyst_rank_em",
        provider_class="AshareEventProvider",
        data_domain="research",
        priority="P3",
        recommendation_stages=("数据采集", "Agent 分析"),
        storage_targets=("evidence", "event_records", "raw_records"),
        factor_groups=("event", "expectation"),
        description="分析师排行，用于长期报告质量增强。",
    ),
    AkshareCapability(
        name="stock_esg_hz_sina",
        provider_class="AshareRiskProvider",
        data_domain="esg",
        priority="P3",
        recommendation_stages=("数据采集", "风险反驳"),
        storage_targets=("risk_findings", "raw_records"),
        factor_groups=("risk", "esg"),
        description="华证 ESG 评级，用于长周期风险和偏好标签。",
    ),
    AkshareCapability(
        name="stock_buffett_index_lg",
        provider_class="AshareMarketBreadthProvider",
        data_domain="market_breadth",
        priority="P3",
        recommendation_stages=("数据采集", "大盘过滤", "仓位建议"),
        storage_targets=("event_records", "risk_findings", "raw_records"),
        factor_groups=("market_breadth", "valuation"),
        description="巴菲特指标，用于市场估值水位和仓位建议。",
    ),
    AkshareCapability(
        name="stock_a_ttm_lyr",
        provider_class="AshareMarketBreadthProvider",
        data_domain="market_breadth",
        priority="P3",
        recommendation_stages=("数据采集", "大盘过滤", "仓位建议"),
        storage_targets=("event_records", "risk_findings", "raw_records"),
        factor_groups=("market_breadth", "valuation"),
        description="A 股等权重与中位数市盈率，用于市场估值水位。",
    ),
)


def iter_capabilities(
    *,
    priority: str | None = None,
    provider_class: str | None = None,
    enabled_in_mvp: bool | None = None,
) -> tuple[AkshareCapability, ...]:
    """按条件查询 AKShare 能力定义。"""

    capabilities = AKSHARE_CAPABILITIES
    if priority is not None:
        capabilities = tuple(item for item in capabilities if item.priority == priority)
    if provider_class is not None:
        capabilities = tuple(item for item in capabilities if item.provider_class == provider_class)
    if enabled_in_mvp is not None:
        capabilities = tuple(item for item in capabilities if item.enabled_in_mvp is enabled_in_mvp)
    return capabilities


def get_capability(name: str) -> AkshareCapability:
    """按接口名读取能力定义。"""

    for capability in AKSHARE_CAPABILITIES:
        if capability.name == name:
            return capability
    raise KeyError(f"未登记 AKShare 接口: {name}")
