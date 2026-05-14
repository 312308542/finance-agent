# 系统架构与模块化设计

本文档是工程实现蓝图，回答三个问题：

- 项目骨架怎么拆。
- 每个模块负责什么、不能碰什么。
- 第三方组件库在哪里调用、怎么调用、输出如何统一。

相关文档：

- `docs/PLAN.md`：总体计划和里程碑。
- `docs/DOMAIN_PROTOCOLS.md`：金融领域模型、信号、推荐、风控、订单草案和证据协议。

## 1. 架构结论

本项目不纯手写，也不直接搬开源项目骨架。第一版工程主线是 **AI 标的推荐**，同时覆盖 A 股和数字货币，不是自动交易系统。

采用 **轻量六边形架构 / Ports and Adapters**：

- 核心领域模型自己设计。
- 应用服务自己编排。
- 金融计算使用成熟库。
- 候选池、数据源、指标库、回测库、绩效库都包在 adapter 后面。
- CLI、FastAPI、定时任务、Dashboard 都复用同一套 application service。

依赖方向必须保持单向：

```mermaid
flowchart TD
    UI["CLI / FastAPI / Scheduler / Dashboard API"] --> APP["Application Services"]
    APP --> PIPE["Asset Recommendation Pipeline"]
    APP --> DOMAIN["Domain Models / Protocols"]
    APP --> PORTS["Ports"]
    PORTS --> ADAPTERS["Adapters"]
    ADAPTERS --> LIBS["AKShare / ccxt / talib / ta / bt / quantstats / SQLAlchemy"]
    APP --> AGENTS["LangGraph Agents"]
    AGENTS --> APP
    APP --> REPORTS["JSON / Markdown / HTML Reports"]
```

规则：

- `domain` 不依赖任何三方金融库。
- `application` 可以调用 `ports` 和 `domain`，不能直接调用 AKShare、ccxt、talib、bt。
- `adapters` 负责三方库调用和字段归一化。
- `agents` 只消费结构化数据，不直接抓取行情，不直接计算因子，不直接打分。
- `execution` 和订单草案属于后续扩展，不进入第一版标的推荐主链路。

标的推荐主链路：

```text
UniverseService
  -> RefreshService
  -> FactorService
  -> ScreeningService
  -> ScoringService
  -> BacktestService
  -> AgentGraph
  -> AssetRecommendationService
  -> ReportWriter
```

## 2. 组件选型

| 层 | 组件 | 角色 | 调用位置 |
| --- | --- | --- | --- |
| 包管理 | uv | Python 依赖和虚拟环境 | 项目根目录 |
| CLI | Typer | Hermes 第一集成入口 | `finance_agent/cli/` |
| API | FastAPI | Dashboard 和后续 MCP 服务基础 | `finance_agent/api/` |
| 配置 | pydantic-settings | 环境变量、密钥路径、数据源开关 | `finance_agent/config/` |
| 数据模型 | Pydantic | CLI/API/Agent 的结构化协议 | `finance_agent/domain/` |
| 数据库 | PostgreSQL + TimescaleDB | 业务数据和行情时序数据 | `finance_agent/storage/` |
| ORM | SQLAlchemy 2.x | 业务表 ORM 和查询封装 | `finance_agent/storage/` |
| 迁移 | Alembic | 数据库 schema 迁移 | `finance_agent/storage/migrations/` |
| 调度 | APScheduler | 本地定时刷新 | `finance_agent/scheduler/` |
| Agent | LangGraph | 多 Agent 编排 | `finance_agent/agents/` |
| A 股数据 | AKShare | 免费 A 股行情、财务、资金流 | `finance_agent/data/providers/akshare_provider.py` |
| 数字货币 | ccxt + Binance API | K 线、成交量、资金费率、未平仓量、多空比例 | `finance_agent/data/providers/ccxt_binance_provider.py`、`finance_agent/data/providers/binance_native_provider.py` |
| 技术指标 | ta-lib-python | 主指标引擎，导入名 `talib` | `finance_agent/indicators/talib_adapter.py` |
| 指标兜底 | ta | 纯 Python 备用指标 | `finance_agent/indicators/ta_fallback_adapter.py` |
| 回测 | bt | 组合回测 | `finance_agent/backtesting/bt_adapter.py` |
| 绩效 | quantstats | 收益、回撤、夏普、HTML 报告 | `finance_agent/performance/quantstats_adapter.py` |
| 前端 | React + Vite | Dashboard | `web/` |
| 前端 UI | Ant Design | 表格、表单、布局、标签、弹窗 | `web/src/components/` |
| 前端请求 | TanStack Query | 服务端状态缓存 | `web/src/api/` |
| 路由 | React Router | 页面路由 | `web/src/routes/` |
| 图表 | ECharts | 组合、风险、回撤、热力图 | `web/src/charts/` |
| K 线 | Lightweight Charts | K 线图 | `web/src/charts/KlineChart.tsx` |

## 3. 目录结构

```text
finance-agent/
  docs/
    PLAN.md
    DOMAIN_PROTOCOLS.md
    ARCHITECTURE.md
  pyproject.toml
  .env.example
  src/
    finance_agent/
      __init__.py
      bootstrap.py

      config/
        settings.py

      domain/
        enums.py
        assets.py
        accounts.py
        market.py
        universe.py
        factors.py
        indicators.py
        signals.py
        risk.py
        recommendations.py
        orders.py
        evidence.py
        reports.py

      ports/
        data.py
        universe.py
        indicators.py
        backtesting.py
        performance.py
        storage.py
        execution.py
        llm.py

      application/
        universe_service.py
        factor_service.py
        screening_service.py
        scoring_service.py
        portfolio_service.py
        asset_service.py
        signal_service.py
        backtest_service.py
        asset_recommendation_service.py
        recommendation_service.py
        trade_service.py
        refresh_service.py

      data/
        router.py
        normalizers.py
        providers/
          akshare_provider.py
          ccxt_binance_provider.py
          binance_native_provider.py

      indicators/
        registry.py
        talib_adapter.py
        ta_fallback_adapter.py

      factors/
        engine.py
        rules/
          fundamental_factors.py
          valuation_factors.py
          technical_factors.py
          capital_flow_factors.py
          event_factors.py

      screening/
        engine.py
        rules.py

      scoring/
        engine.py
        weighting.py

      signals/
        engine.py
        rules/
          technical_rules.py
          fundamental_rules.py
          capital_flow_rules.py
          event_rules.py

      backtesting/
        bt_adapter.py
        strategy_templates.py

      performance/
        quantstats_adapter.py

      risk/
        service.py
        rules.py

      recommendations/
        service.py
        policy.py
        asset_recommendation_service.py

      execution/
        order_draft_service.py
        validation.py
        binance_execution.py

      agents/
        state.py
        graph.py
        nodes/
          fundamental_analyst_node.py
          technical_analyst_node.py
          event_analyst_node.py
          risk_rebuttal_node.py
          flow_derivatives_analyst_node.py
          recommendation_decision_node.py

      storage/
        db.py
        orm.py
        repositories.py
        migrations/

      reports/
        json_report.py
        markdown_report.py
        html_report.py

      scheduler/
        jobs.py
        runner.py

      cli/
        app.py
        commands/
          portfolio.py
          asset.py
          signals.py
          backtest.py
          recommend.py
          trade.py
          dashboard.py

      api/
        app.py
        dependencies.py
        routes/
          portfolio.py
          assets.py
          signals.py
          recommendations.py
          backtests.py
          orders.py
          system.py

  web/
    package.json
    src/
      api/
      routes/
      pages/
      components/
      charts/
      layouts/
      types/

  tests/
    unit/
    integration/
    contract/
```

## 4. 启动与依赖注入

所有入口都通过 `bootstrap.py` 创建容器，不在模块顶层创建真实数据源连接。

```python
# src/finance_agent/bootstrap.py
from finance_agent.config.settings import Settings
from finance_agent.storage.db import create_session_factory
from finance_agent.data.router import DataRouter
from finance_agent.application.portfolio_service import PortfolioService


class AppContainer:
    """应用依赖容器，CLI、API、Scheduler 共用。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session_factory = create_session_factory(settings.database_url)
        self.data_router = DataRouter.from_settings(settings)
        self.portfolio_service = PortfolioService(
            session_factory=self.session_factory,
            data_router=self.data_router,
        )


def build_container() -> AppContainer:
    settings = Settings()
    return AppContainer(settings)
```

这样做的好处：

- CLI 和 FastAPI 不会各自初始化一套逻辑。
- 测试可以替换 fake provider。
- 后续 MCP 也可以复用同一个 container。

## 5. 配置模块

使用 `pydantic-settings`。

```python
# src/finance_agent/config/settings.py
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """系统配置，支持 .env 和环境变量。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FINANCE_AGENT_",
        extra="ignore",
    )

    database_url: str = "sqlite:///./finance_agent.db"
    output_dir: str = "./outputs"

    akshare_enabled: bool = True
    binance_enabled: bool = False
    binance_api_key: str | None = None
    binance_api_secret: str | None = None

    trading_enabled: bool = False
    trading_mode: str = Field(default="simulation", pattern="^(simulation|testnet|live)$")
```

配置原则：

- `trading_enabled` 默认必须是 `False`。
- 密钥不写进文档和代码。
- live 实盘模式必须额外确认。

## 6. CLI 架构

使用 Typer 多命令结构。CLI 只解析参数和输出文件，不写业务逻辑。

```python
# src/finance_agent/cli/app.py
import typer

from finance_agent.cli.commands import asset, backtest, factors, recommend, signals, universe

app = typer.Typer(no_args_is_help=True)
app.add_typer(universe.app, name="universe")
app.add_typer(factors.app, name="factors")
app.add_typer(asset.app, name="asset")
app.add_typer(signals.app, name="signals")
app.add_typer(backtest.app, name="backtest")
app.add_typer(recommend.app, name="recommend")
```

命令调用应用服务：

```python
# src/finance_agent/cli/commands/recommend.py
import typer

from finance_agent.bootstrap import build_container
from finance_agent.reports.json_report import write_result_json
from finance_agent.reports.markdown_report import write_report_md

app = typer.Typer()


@app.command("assets")
def recommend_assets(
    universe: str = typer.Option("hs300", help="候选池，例如 hs300、all_ashare、binance_spot_top"),
    strategy: str = typer.Option("balanced_growth", help="推荐策略"),
    limit: int = typer.Option(10, help="推荐数量"),
    output_dir: str = typer.Option("./outputs", help="输出目录"),
):
    """生成 A 股/数字货币推荐榜，并输出 result.json 和 report.md。"""
    container = build_container()
    result = container.asset_recommendation_service.recommend(
        universe=universe,
        strategy=strategy,
        limit=limit,
    )
    write_result_json(result, output_dir / "result.json")
    write_report_md(result, output_dir / "report.md")
```

CLI 约束：

- 不直接访问数据库。
- 不直接调用 AKShare、ccxt、talib。
- 不直接调用 LLM。
- 输出必须同时包含 `result.json` 和 `report.md`。

## 7. FastAPI 架构

FastAPI 只作为 Dashboard API。第一版不把它做成复杂微服务。

```python
# src/finance_agent/api/app.py
from fastapi import FastAPI

from finance_agent.api.routes import assets, factors, recommendations, signals, universe


def create_app() -> FastAPI:
    app = FastAPI(title="Hermes Finance Agent API")
    app.include_router(universe.router, prefix="/api/universe", tags=["universe"])
    app.include_router(assets.router, prefix="/api/assets", tags=["assets"])
    app.include_router(factors.router, prefix="/api/factors", tags=["factors"])
    app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
    app.include_router(recommendations.router, prefix="/api/recommendations", tags=["recommendations"])
    return app
```

路由调用应用服务：

```python
# src/finance_agent/api/routes/recommendations.py
from fastapi import APIRouter, Depends

from finance_agent.api.dependencies import get_container

router = APIRouter()


@router.get("/assets")
def get_asset_recommendations(
    universe: str = "hs300",
    strategy: str = "balanced_growth",
    limit: int = 10,
    container=Depends(get_container),
):
    """返回 A 股/数字货币推荐榜。"""
    return container.asset_recommendation_service.recommend(universe, strategy, limit)
```

API 约束：

- 返回 Pydantic 模型或可序列化 DTO。
- 不返回 ORM 对象。
- 不在路由里写计算逻辑。
- 第一版不暴露订单确认接口。

## 8. 定时任务架构

第一版使用 APScheduler，本地进程内运行。后续需要分布式时再换 Celery。

```python
# src/finance_agent/scheduler/runner.py
from apscheduler.schedulers.background import BackgroundScheduler

from finance_agent.bootstrap import build_container


def start_scheduler() -> BackgroundScheduler:
    """启动本地定时刷新任务。"""
    container = build_container()
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    scheduler.add_job(
        container.refresh_service.refresh_daily_market_data,
        trigger="cron",
        hour=18,
        minute=30,
        id="refresh_ashare_daily",
        replace_existing=True,
    )

    scheduler.add_job(
        container.refresh_service.refresh_crypto_market_data,
        trigger="interval",
        minutes=30,
        id="refresh_crypto_30m",
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
```

任务拆分：

- `refresh_ashare_daily`：A 股日线、指数、板块、财务、资金流。
- `refresh_crypto_30m`：币安 K 线、资金费率、未平仓量。
- `compute_signals_after_refresh`：刷新后计算指标和信号。
- `cleanup_expired_order_drafts`：清理过期订单草案。

## 9. 存储架构

全环境统一使用 PostgreSQL + TimescaleDB。业务实体使用普通 PostgreSQL 表，K 线和数字货币衍生品快照使用 TimescaleDB hypertable。开发、测试、演示和生产保持同一套数据库能力，不提供 SQLite 或普通 PostgreSQL 降级模式，避免本地绕过 hypertable、唯一约束和压缩策略。领域模型和 ORM 模型分开，避免数据库字段污染业务协议。

详细表结构、主键、索引和建表优先级见：`docs/DATABASE_DESIGN.md`。本节只说明存储层原则和示例。

```python
# src/finance_agent/storage/db.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def create_session_factory(database_url: str):
    engine = create_engine(database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
```

```python
# src/finance_agent/storage/orm.py
from datetime import datetime
from sqlalchemy import DateTime, String, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SignalSnapshotORM(Base):
    """信号快照表，payload 保存协议原文，便于版本升级。"""

    __tablename__ = "signal_snapshots"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(64), index=True)
    horizon: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
```

第一版表设计：

| 表 | 用途 |
| --- | --- |
| `raw_records` | 原始数据响应 |
| `assets` | 资产主数据 |
| `market_bars` | 标准 OHLCV |
| `fundamental_snapshots` | A 股财务估值快照 |
| `account_snapshots` | 账户快照 |
| `positions` | 持仓快照 |
| `indicator_frames` | 指标计算结果 |
| `feature_frames` | 特征结果 |
| `signal_snapshots` | 信号快照 |
| `risk_findings` | 风险发现 |
| `recommendations` | 推荐建议 |
| `order_drafts` | 订单草案 |
| `evidence` | 证据 |
| `analysis_runs` | 每次分析运行记录 |

存储策略：

- 结构化检索字段单独建列。
- 复杂协议全文放 `payload JSON`。
- 每个 payload 记录 `schema_version`。
- `raw_records` 永远不覆盖，便于审计。

## 10. 数据 Provider 架构

### 10.1 Port 定义

```python
# src/finance_agent/ports/data.py
from typing import Protocol

from finance_agent.domain.market import MarketData
from finance_agent.domain.accounts import AccountSnapshot, Position


class MarketDataProvider(Protocol):
    """行情数据 Provider 接口。"""

    provider_name: str

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: str | None, end: str | None) -> MarketData:
        ...


class AccountProvider(Protocol):
    """账户数据 Provider 接口。"""

    provider_name: str

    def fetch_account_snapshot(self) -> AccountSnapshot:
        ...

    def fetch_positions(self) -> list[Position]:
        ...
```

### 10.2 AKShare 调用

AKShare 只在 adapter 中调用。

```python
# src/finance_agent/data/providers/akshare_provider.py
import akshare as ak

from finance_agent.domain.market import MarketData
from finance_agent.data.normalizers import normalize_ashare_hist


class AkshareProvider:
    provider_name = "akshare"

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: str | None, end: str | None) -> MarketData:
        """获取 A 股历史行情，并转换成统一 MarketData。"""
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period=self._to_ak_period(timeframe),
            start_date=start or "20000101",
            end_date=end or "20991231",
            adjust="qfq",
        )
        return normalize_ashare_hist(symbol=symbol, timeframe=timeframe, df=df, source=self.provider_name)
```

归一化规则：

- 中文列名统一转为 `open/high/low/close/volume/amount`。
- 日期统一转为带时区的 ISO 时间。
- 前复权、后复权、未复权必须写入 metadata。
- AKShare 返回空表时，状态为 `unavailable`，不能假装可用。

### 10.3 ccxt / Binance 调用

ccxt 用于交易所统一接口，主要读取 markets、OHLCV、ticker 这类跨交易所通用数据。Binance 原生 Provider 只补充 U 本位合约专属公开行情，例如资金费率、标记价/指数价、未平仓量和多空账户比；它不包含账户、持仓或下单能力。

```python
# src/finance_agent/data/providers/ccxt_binance_provider.py
import ccxt
import pandas as pd

from finance_agent.domain.market import MarketData
from finance_agent.data.normalizers import normalize_crypto_ohlcv


class CcxtBinanceProvider:
    provider_name = "ccxt_binance"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, default_type: str = "spot"):
        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": default_type},
        })

    def fetch_ohlcv(self, symbol: str, timeframe: str, start: str | None, end: str | None) -> MarketData:
        """获取数字货币 K 线。"""
        since_ms = self._to_milliseconds(start) if start else None
        rows = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=1000)
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        return normalize_crypto_ohlcv(symbol=symbol, timeframe=timeframe, df=df, source=self.provider_name)

    def fetch_balance(self) -> dict:
        """获取账户余额，原始结果只能在 adapter 内部处理。"""
        return self.exchange.fetch_balance()
```

当前项目里 `CcxtBinanceProvider` 只实现公开行情读取，不实现账户余额读取。衍生品补充数据使用单独的 Binance 原生 Provider：

```python
# src/finance_agent/data/providers/binance_native_provider.py
class BinanceNativeProvider:
    """读取 Binance U 本位合约专属公开行情。"""

    provider_name = "binance_native"

    def fetch_derivative_snapshot(self, symbol: str):
        """返回资金费率、未平仓量、多空比等衍生品快照。"""
        ...
```

交易调用只允许在 `execution` 中出现：

```python
# src/finance_agent/execution/binance_execution.py
class BinanceExecutionProvider:
    def submit_order_draft(self, draft):
        """提交已经二次确认的订单草案。"""
        if draft.validation.status not in {"ok", "warning", "blocked"}:
            raise ValueError("订单草案状态不允许提交")
        if not draft.confirmed_at:
            raise ValueError("订单草案未经过用户二次确认")

        return self.exchange.create_order(
            symbol=draft.symbol,
            type=draft.order_type,
            side=draft.side,
            amount=draft.quantity,
            price=draft.limit_price,
            params=self._build_params(draft),
        )
```

执行安全规则：

- Provider 可以读数据，不能下单。
- ExecutionProvider 可以下单，但只能接收 `confirmed` 的 `OrderDraft`。
- live 模式必须检查 `trading_enabled=True`。
- 所有交易响应必须写入审计日志。

## 11. 指标模块

### 11.1 Port 定义

```python
# src/finance_agent/ports/indicators.py
from typing import Protocol

from finance_agent.domain.market import MarketData
from finance_agent.domain.indicators import IndicatorFrame


class IndicatorAdapter(Protocol):
    """技术指标适配器。"""

    library_name: str

    def compute(self, market_data: MarketData, indicators: list[str]) -> IndicatorFrame:
        ...
```

### 11.2 ta-lib-python 调用

`ta-lib-python` 的 PyPI 包名是 `TA-Lib`，代码里导入 `talib`。

```python
# src/finance_agent/indicators/talib_adapter.py
import talib

from finance_agent.domain.indicators import IndicatorFrame


class TalibIndicatorAdapter:
    library_name = "talib"

    def compute(self, market_data, indicators: list[str]) -> IndicatorFrame:
        """使用 talib 计算主技术指标。"""
        close = market_data.to_series("close")
        high = market_data.to_series("high")
        low = market_data.to_series("low")

        values = {}
        if "rsi_14" in indicators:
            values["rsi_14"] = talib.RSI(close, timeperiod=14).iloc[-1]
        if "macd" in indicators:
            macd, signal, hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
            values["macd"] = macd.iloc[-1]
            values["macd_signal"] = signal.iloc[-1]
            values["macd_hist"] = hist.iloc[-1]
        if "atr_14" in indicators:
            values["atr_14"] = talib.ATR(high, low, close, timeperiod=14).iloc[-1]
        if "bbands_20" in indicators:
            upper, middle, lower = talib.BBANDS(close, timeperiod=20)
            values["bb_upper"] = upper.iloc[-1]
            values["bb_middle"] = middle.iloc[-1]
            values["bb_lower"] = lower.iloc[-1]

        return IndicatorFrame.from_values(
            market_data=market_data,
            library=self.library_name,
            values=values,
        )
```

注意事项：

- TA-Lib 初始 lookback 会产生 NaN，不能简单填 0。
- 输入数据有 NaN 时要先标记数据质量。
- 输出必须记录 `library`、`library_version`、`input_window`。
- K 线形态识别后续可通过 `talib.get_function_groups()["Pattern Recognition"]` 批量注册。

### 11.3 ta 兜底调用

```python
# src/finance_agent/indicators/ta_fallback_adapter.py
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volatility import AverageTrueRange, BollingerBands


class TaIndicatorFallbackAdapter:
    library_name = "ta"

    def compute(self, market_data, indicators: list[str]):
        """TA-Lib 不可用时的纯 Python 兜底。"""
        close = market_data.to_series("close")
        high = market_data.to_series("high")
        low = market_data.to_series("low")

        values = {}
        if "rsi_14" in indicators:
            values["rsi_14"] = RSIIndicator(close=close, window=14).rsi().iloc[-1]
        if "macd" in indicators:
            macd = MACD(close=close)
            values["macd"] = macd.macd().iloc[-1]
            values["macd_signal"] = macd.macd_signal().iloc[-1]
            values["macd_hist"] = macd.macd_diff().iloc[-1]
        if "atr_14" in indicators:
            values["atr_14"] = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range().iloc[-1]
        if "bbands_20" in indicators:
            bb = BollingerBands(close=close, window=20)
            values["bb_upper"] = bb.bollinger_hband().iloc[-1]
            values["bb_middle"] = bb.bollinger_mavg().iloc[-1]
            values["bb_lower"] = bb.bollinger_lband().iloc[-1]

        return values
```

兜底规则：

- 默认先调用 `TalibIndicatorAdapter`。
- 如果导入失败或环境不支持，切换到 `TaIndicatorFallbackAdapter`。
- 两者在测试样例中做容差对比。

## 12. 信号模块

信号模块只读 `IndicatorFrame`、`FeatureFrame`、`Position`、`RiskFinding`，不直接调用第三方库。

```python
# src/finance_agent/signals/engine.py
class SignalEngine:
    """把指标、特征和组合信息转换成信号快照。"""

    def __init__(self, rules):
        self.rules = rules

    def compute_asset_signal(self, context):
        groups = []
        for rule in self.rules:
            groups.append(rule.evaluate(context))
        return self._merge_groups(context.asset, groups)
```

规则模块：

- `technical_rules.py`：趋势、动量、突破、波动。
- `fundamental_rules.py`：估值、财务质量、成长。
- `derivatives_rules.py`：资金费率、未平仓量、多空比。
- `portfolio_rules.py`：仓位、相关性、集中度。

合并规则：

- 分组先各自打分。
- 按市场和周期选择权重。
- 缺失分组不参与强行平均，状态降级为 `partial`。
- 严重缺失写入 `missing_data`。

## 13. 回测模块

bt 只在 `backtesting/bt_adapter.py` 中调用。

```python
# src/finance_agent/backtesting/bt_adapter.py
import bt


class BtBacktestAdapter:
    """组合回测适配器。"""

    def run_momentum_template(self, prices, rebalance="monthly"):
        """运行动量策略模板，prices 是宽表 close price DataFrame。"""
        run_algo = bt.algos.RunMonthly() if rebalance == "monthly" else bt.algos.RunWeekly()

        strategy = bt.Strategy(
            "momentum_v1",
            [
                run_algo,
                bt.algos.SelectAll(),
                bt.algos.WeighEqually(),
                bt.algos.Rebalance(),
            ],
        )
        backtest = bt.Backtest(strategy, prices)
        result = bt.run(backtest)
        return self._to_backtest_result(result)
```

输入要求：

- `prices` 必须是 pandas DataFrame。
- index 是交易日期。
- columns 是资产代码。
- values 是复权收盘价或统一 close。

输出统一为 `BacktestResult`：

- 净值曲线。
- 回撤曲线。
- 总收益。
- 年化收益。
- 最大回撤。
- 换手率。
- 策略参数。
- 数据版本。
- 信号版本。

## 14. 绩效模块

quantstats 只在 `performance/quantstats_adapter.py` 中调用。

```python
# src/finance_agent/performance/quantstats_adapter.py
from pathlib import Path
import quantstats as qs


class QuantStatsAdapter:
    """收益序列绩效分析适配器。"""

    def analyze(self, returns, benchmark=None, output_html: Path | None = None):
        """输入收益率序列，输出统一绩效报告。"""
        stats = {
            "sharpe": qs.stats.sharpe(returns),
            "sortino": qs.stats.sortino(returns),
            "cagr": qs.stats.cagr(returns),
            "max_drawdown": qs.stats.max_drawdown(returns),
            "volatility": qs.stats.volatility(returns),
            "win_rate": qs.stats.win_rate(returns),
        }

        if output_html:
            qs.reports.html(returns, benchmark=benchmark, output=str(output_html))

        return self._to_performance_report(stats, output_html)
```

注意：

- quantstats 的胜率是周期收益维度，不是逐笔交易胜率。
- HTML 报告只作为附件，不作为系统主协议。
- 主协议仍是 `PerformanceReport`。

## 15. Agent 架构

LangGraph 负责编排，不负责替代确定性计算。第一版保留贴近 A 股和数字货币分析职责的 6 个 Agent，数据、因子、初筛、评分和回测都在进入 Agent 前完成。

```python
# src/finance_agent/agents/graph.py
from langgraph.graph import StateGraph, START, END

from finance_agent.agents.state import AssetRecommendationState
from finance_agent.agents.nodes import (
    event_analyst_node,
    flow_derivatives_analyst_node,
    fundamental_analyst_node,
    recommendation_decision_node,
    risk_rebuttal_node,
    technical_analyst_node,
)


def build_asset_recommendation_graph():
    """构建 A 股/数字货币推荐 Agent 图。"""
    graph = StateGraph(AssetRecommendationState)

    graph.add_node("fundamental", fundamental_analyst_node.run)
    graph.add_node("technical", technical_analyst_node.run)
    graph.add_node("flow_derivatives", flow_derivatives_analyst_node.run)
    graph.add_node("event", event_analyst_node.run)
    graph.add_node("risk_rebuttal", risk_rebuttal_node.run)
    graph.add_node("recommendation_decision", recommendation_decision_node.run)

    graph.add_edge(START, "fundamental")
    graph.add_edge("fundamental", "technical")
    graph.add_edge("technical", "flow_derivatives")
    graph.add_edge("flow_derivatives", "event")
    graph.add_edge("event", "risk_rebuttal")
    graph.add_edge("risk_rebuttal", "recommendation_decision")
    graph.add_edge("recommendation_decision", END)

    return graph.compile()
```

`AssetRecommendationState`：

```python
# src/finance_agent/agents/state.py
from typing import TypedDict


class AssetRecommendationState(TypedDict, total=False):
    """A 股/数字货币推荐 Agent 工作流状态。"""

    run_id: str
    strategy: str
    universe: dict
    factor_frames: list[dict]
    asset_scores: list[dict]
    signals: list[dict]
    backtests: list[dict]
    risks: list[dict]
    evidence: list[dict]
    candidate_summaries: list[dict]
    asset_recommendations: list[dict]
    recommendation_rank: dict
    unavailable_data: list[dict]
    final_report: str
```

Agent 规则：

- `fundamental_analyst_node` 解释 A 股基本面、估值和行业，也解释数字货币项目面、生态和公开基本信息，不改因子分。
- `technical_analyst_node` 只解释技术面信号，不重新计算 RSI、MACD 等指标。
- `flow_derivatives_analyst_node` 解释 A 股资金流，也解释数字货币资金费率、未平仓量、多空比例和合约拥挤度。
- `event_analyst_node` 只解释公告、新闻、资金流和板块事件。
- `risk_rebuttal_node` 必须生成反方观点和失效条件。
- `recommendation_decision_node` 必须引用 `score_id`、`signal_ids`、`risk_ids` 和 `evidence_ids`。
- LLM 不允许发明价格、财务指标、链上指标、资金费率、交易结果，不允许直接修改 `AssetScore.total_score`。

## 16. 标的推荐模块

### 16.1 推荐排序服务

```python
# src/finance_agent/recommendations/asset_recommendation_service.py
class AssetRecommendationService:
    """根据评分、信号、风险反驳和 Agent 摘要生成推荐榜。"""

    def rank(self, context):
        recommendations = []
        for candidate in context.candidates:
            action = self.policy.decide_action(
                score=candidate.asset_score,
                risks=candidate.risks,
                agent_summary=candidate.agent_summary,
            )
            recommendations.append(self._build_asset_recommendation(candidate, action))
        return self._build_rank(recommendations)
```

推荐排序服务不抓数据、不计算指标、不下单，只负责把已经生成的结构化结果整理成 `AssetRecommendation` 和 `RecommendationRank`。

### 16.2 后续交易扩展

订单草案和实盘提交不进入第一版主链路。后续如需接入交易，只能基于已经生成的推荐结果生成 `OrderDraft`，且必须经过用户二次确认。

## 17. 应用服务设计

应用服务是一切入口复用的核心。

| Service | 入口 | 主要输出 |
| --- | --- | --- |
| `UniverseService` | Scheduler、CLI、API | AssetUniverse |
| `RefreshService` | Scheduler、CLI | RawRecord、Canonical 数据 |
| `FactorService` | Scheduler、CLI、API | FactorFrame |
| `ScreeningService` | CLI、API | ScreeningResult |
| `ScoringService` | CLI、API | AssetScore |
| `SignalService` | Scheduler、CLI、API | SignalSnapshot |
| `BacktestService` | CLI、API、Agent | BacktestResult |
| `PerformanceService` | BacktestService、AssetRecommendationService | PerformanceReport |
| `AssetService` | CLI、API | 单资产分析 |
| `AssetRecommendationService` | Agent、CLI、API | AssetRecommendation、RecommendationRank |
| `PortfolioService` | CLI、API、Agent | 后续持仓辅助分析 |
| `RecommendationService` | Agent、CLI、API | 通用 Recommendation，后续兼容多资产 |
| `TradeService` | CLI、API | 后续 OrderDraft、提交结果 |

典型调用链：

```text
CLI recommend assets
  -> UniverseService.build_or_load
    -> RefreshService.ensure_fresh_data
    -> FactorService.compute_for_universe
    -> ScreeningService.apply_rules
    -> ScoringService.score_candidates
    -> SignalService.compute_for_candidates
    -> BacktestService.run_topn_templates
    -> AgentGraph.invoke
    -> AssetRecommendationService.rank
    -> ReportWriter.write_json/write_md
```

服务边界：

- `UniverseService` 只负责候选池来源、成分和过滤元信息，不计算标的好坏。
- `FactorService` 只负责把行情、财务、估值、资金流、事件转换成因子值。
- `ScreeningService` 只执行硬过滤，例如 ST、停牌、流动性不足、数据严重缺失。
- `ScoringService` 使用透明权重生成分数，不调用 LLM。
- `SignalService` 把因子和指标转成可解释信号，不直接输出最终推荐。
- `AgentGraph` 只解释、比较、反驳和综合，不抓数据、不算指标、不编分数。
- `AssetRecommendationService` 负责推荐排序、观察池、回避池和最终输出协议。

## 18. 前端 Dashboard 架构

前端不是营销页，是工作台。

详细交互和视觉规范见：`docs/UI_UX_GUIDELINES.md`。

### 18.1 技术栈

- React + Vite。
- Ant Design：布局、表格、表单、状态标签、弹窗。
- TanStack Query：请求、缓存、刷新。
- React Router：页面路由。
- ECharts：统计图。
- Lightweight Charts：K 线图。

### 18.1.1 React 实现约束

这些规则来自 `ui-ux-pro-max` 的 React stack 检索，并结合本项目调整：

- 路由页使用 lazy loading。
- K 线、热力图、回测报告等重图表按页面或功能懒加载。
- 大表格和 Agent 执行日志超过 100 行时使用虚拟化。
- 表格筛选、排序、分组、聚合使用 memo 化，避免每次渲染重新计算。
- 服务端状态统一交给 TanStack Query，避免手写重复 loading/error/cache。
- Modal 必须有 focus 管理，订单确认关闭后焦点回到触发按钮。
- Loading 超过 300ms 必须显示 skeleton、spinner 或按钮 loading。
- 空状态必须有解释和下一步动作，不能空白。
- 图表必须提供文本摘要或表格 fallback。

### 18.2 目录结构

```text
web/src/
  api/
    client.ts
    portfolio.ts
    signals.ts
    recommendations.ts
    orders.ts
  routes/
    router.tsx
  layouts/
    AppLayout.tsx
  pages/
    OverviewPage.tsx
    AssetRecommendationsPage.tsx
    UniversePage.tsx
    StockDetailPage.tsx
    FactorsPage.tsx
    AssetPage.tsx
    SignalCenterPage.tsx
    RiskCenterPage.tsx
    BacktestPage.tsx
    EvidencePage.tsx
    SettingsPage.tsx
  components/
    RiskTag.tsx
    SignalScore.tsx
    AssetScoreBreakdown.tsx
    AssetRecommendationPanel.tsx
  charts/
    FactorRadar.tsx
    DrawdownChart.tsx
    EquityCurve.tsx
    CorrelationHeatmap.tsx
    KlineChart.tsx
```

### 18.3 API 调用

```typescript
// web/src/api/recommendations.ts
import { useQuery } from "@tanstack/react-query";
import { http } from "./client";

export function useAssetRecommendations(universe: string, strategy: string) {
  return useQuery({
    queryKey: ["asset-recommendations", universe, strategy],
    queryFn: async () => {
      const res = await http.get("/api/recommendations/assets", {
        params: { universe, strategy },
      });
      return res.data;
    },
    staleTime: 30_000,
  });
}
```

### 18.4 页面路由

```typescript
// web/src/routes/router.tsx
import { createBrowserRouter } from "react-router-dom";
import { AppLayout } from "../layouts/AppLayout";
import { OverviewPage } from "../pages/OverviewPage";
import { AssetRecommendationsPage } from "../pages/AssetRecommendationsPage";
import { UniversePage } from "../pages/UniversePage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: "recommendations", element: <AssetRecommendationsPage /> },
      { path: "universe", element: <UniversePage /> },
    ],
  },
]);
```

### 18.5 Ant Design 使用

```tsx
// web/src/pages/AssetRecommendationsPage.tsx
import { Alert, Table, Tag } from "antd";
import { useAssetRecommendations } from "../api/recommendations";

export function AssetRecommendationsPage() {
  const { data, isLoading } = useAssetRecommendations("hs300", "balanced_growth");

  return (
    <>
      {data?.human_readable_summary && (
        <Alert type="info" message={data.human_readable_summary} showIcon />
      )}
      <Table
        loading={isLoading}
        rowKey="recommendation_id"
        dataSource={data?.asset_recommendations ?? []}
        columns={[
          { title: "排名", dataIndex: "rank" },
          { title: "标的", dataIndex: "symbol" },
          { title: "市场", dataIndex: "market" },
          { title: "综合分", dataIndex: "total_score" },
          { title: "动作", dataIndex: "action" },
          {
            title: "风险",
            dataIndex: "risk_level",
            render: (value) => <Tag color={value === "high" ? "red" : "blue"}>{value}</Tag>,
          },
        ]}
      />
    </>
  );
}
```

### 18.6 ECharts 调用

```tsx
// web/src/charts/EquityCurve.tsx
import * as echarts from "echarts";
import { useEffect, useRef } from "react";

export function EquityCurve({ points }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: points.map((p) => p.date) },
      yAxis: { type: "value" },
      series: [{ type: "line", data: points.map((p) => p.value), showSymbol: false }],
    });
    return () => chart.dispose();
  }, [points]);

  return <div ref={ref} style={{ height: 320 }} />;
}
```

### 18.7 Lightweight Charts 调用

```tsx
// web/src/charts/KlineChart.tsx
import { createChart, CandlestickSeries } from "lightweight-charts";
import { useEffect, useRef } from "react";

export function KlineChart({ bars }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, { height: 360 });
    const candles = chart.addSeries(CandlestickSeries);
    candles.setData(
      bars.map((bar) => ({
        time: bar.date,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })),
    );
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [bars]);

  return <div ref={ref} />;
}
```

## 19. 报告输出

报告模块只负责格式化，不负责计算。

输出：

- `result.json`：结构化结果，给 Hermes、Dashboard、自动化流程。
- `report.md`：中文摘要，给用户阅读。
- `quantstats.html`：绩效附件，给回测中心打开。

`result.json` 必须符合 `DOMAIN_PROTOCOLS.md`。

## 20. 错误处理与降级

统一异常：

| 异常 | 场景 |
| --- | --- |
| `ProviderUnavailableError` | 数据源不可用 |
| `DataStaleError` | 数据过期 |
| `DataValidationError` | 字段缺失或格式错误 |
| `IndicatorComputationError` | 指标计算失败 |
| `BacktestError` | 回测失败 |
| `RiskBlockedError` | 风控阻断 |
| `OrderValidationError` | 订单参数无效 |

降级原则：

- 数据源失败：换 fallback provider。
- 指标主库失败：换 `ta` 兜底。
- 新闻失败：事件信号 `unavailable`，推荐提示缺失。
- 回测失败：推荐可生成，但必须降低置信度。
- 风控阻断：订单草案状态变为 `blocked` 或 `invalid`。

## 21. 测试架构

测试分层：

```text
unit/
  测领域模型、规则、字段转换、风险校验
integration/
  测 AKShare、ccxt、talib、bt、quantstats adapter
contract/
  测 CLI result.json、API response、Dashboard 类型协议
```

重点测试：

- TA-Lib 和 ta 核心指标容差。
- AKShare 中文列名归一化。
- ccxt OHLCV 转换。
- bt 回测输出稳定。
- quantstats 周期胜率口径说明。
- 推荐必须引用信号和风险。
- 订单草案未确认不能提交。

## 22. 第一阶段实现顺序

### M0A：工程骨架

1. `pyproject.toml`。
2. `src/finance_agent` 目录。
3. `Settings`。
4. `AppContainer`。
5. Typer CLI 空命令。
6. FastAPI 空服务。
7. `result.json` 和 `report.md` writer。

已落地：

- `pyproject.toml` 和 `src/finance_agent` 基础包。
- `docker-compose.yml` 启动 PostgreSQL + TimescaleDB。
- `src/finance_agent/storage/db.py` 数据库连接工厂。
- `src/finance_agent/storage/orm.py` M0 ORM 模型。
- `src/finance_agent/storage/migrations/versions/20260514_0001_create_m0_schema.py` M0 Alembic 迁移。
- `docs/DATABASE_SETUP.md` 数据库启动和迁移说明。

### M0B：领域模型

1. 按 `DOMAIN_PROTOCOLS.md` 建 Pydantic 模型。
2. 建枚举。
3. 建 DTO 序列化测试。

### M1A：候选池与数据层

1. `UniverseProvider` port。
2. `MarketDataProvider` port。
3. `AkshareProvider`。
4. `CcxtBinanceProvider`。
5. `AssetUniverse` 构建和存储。
6. RawRecord 和 MarketData 存储。

### M2A：因子、指标与信号

1. `TalibIndicatorAdapter`。
2. `TaIndicatorFallbackAdapter`。
3. `IndicatorRegistry`。
4. `FactorService`。
5. A 股基本面、估值、技术面、资金流和事件因子。
6. 数字货币项目面、技术面、衍生品、流动性和事件因子。
7. `SignalEngine`。
8. 技术信号规则。

### M3A：筛选、评分、回测与绩效

1. `ScreeningService`。
2. `ScoringService`。
3. `BtBacktestAdapter`。
4. `QuantStatsAdapter`。
5. 回测报告写入。

### M4A：Agent 标的推荐

1. `AnalysisState`。
2. 基本面/项目面分析师 Agent。
3. 技术面分析师 Agent。
4. 资金流/衍生品分析师 Agent。
5. 市场事件分析师 Agent。
6. 风险反驳 Agent。
7. 推荐决策 Agent。
8. `AssetRecommendationService`。
9. 推荐榜和中文报告输出。

## 23. 外部文档依据

本设计参考了以下组件官方文档或项目文档：

- Typer 多命令 CLI。
- FastAPI `APIRouter` 多文件应用。
- LangGraph `StateGraph` 工作流。
- APScheduler 本地定时任务。
- SQLAlchemy 2.x ORM。
- pydantic-settings 配置管理。
- TA-Lib Python wrapper 的 Function API 和 Abstract API。
- bt 的 `Strategy`、`Algos`、`Backtest`、`bt.run`。
- quantstats 的 `stats`、`plots`、`reports`。
- ccxt 统一交易所 API。
- AKShare A 股接口。
- Ant Design 组件体系。
- TanStack Query 服务端状态缓存。
- React Router 数据路由。
- ECharts 图表实例。
- Lightweight Charts K 线图。
