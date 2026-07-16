"""第三方数据源 Provider。"""

from finance_agent.data.providers.akshare_fund_provider import AkshareFundProvider
from finance_agent.data.providers.akshare_p1_provider import (
    AshareCapitalFlowProvider,
    AshareEventProvider,
    AshareSectorProvider,
)
from finance_agent.data.providers.akshare_p2_provider import (
    AshareFundamentalProvider,
    AshareValuationProvider,
)
from finance_agent.data.providers.akshare_provider import AkshareProvider
from finance_agent.data.providers.akshare_risk_sentiment_provider import (
    AshareRiskProvider,
    AshareSentimentProvider,
)
from finance_agent.data.providers.binance_native_provider import BinanceNativeProvider
from finance_agent.data.providers.ccxt_binance_provider import CcxtBinanceProvider
from finance_agent.data.providers.eastmoney_article_fetcher import (
    ArticleFetchResult,
    EastmoneyArticleFetcher,
)

__all__ = [
    "AkshareProvider",
    "AkshareFundProvider",
    "ArticleFetchResult",
    "AshareCapitalFlowProvider",
    "AshareEventProvider",
    "AshareFundamentalProvider",
    "AshareRiskProvider",
    "AshareSectorProvider",
    "AshareSentimentProvider",
    "AshareValuationProvider",
    "BinanceNativeProvider",
    "CcxtBinanceProvider",
    "EastmoneyArticleFetcher",
]
