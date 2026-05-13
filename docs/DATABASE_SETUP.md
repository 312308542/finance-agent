# 数据库环境搭建

本文档说明本地和其他环境如何启动 PostgreSQL + TimescaleDB，并执行 M0 数据库迁移。

## 1. 启动数据库

```bash
docker compose up -d postgres
```

默认连接信息：

```text
host: localhost
port: 5432
database: finance_agent
user: finance_agent
password: finance_agent
```

容器初始化脚本会执行：

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

## 2. 安装后端依赖

```bash
python -m pip install -e ".[dev]"
```

## 3. 执行迁移

```bash
alembic upgrade head
```

连接地址可通过环境变量覆盖：

```bash
export FINANCE_AGENT_DATABASE_URL=postgresql+psycopg://finance_agent:finance_agent@localhost:5432/finance_agent
```

Windows PowerShell：

```powershell
$env:FINANCE_AGENT_DATABASE_URL = "postgresql+psycopg://finance_agent:finance_agent@localhost:5432/finance_agent"
alembic upgrade head
```

## 4. 验证仓储层

数据库启动并迁移完成后，可以运行仓储层冒烟脚本：

```bash
python scripts/storage/smoke_repositories.py
```

脚本会写入：

- 一个 A 股样例资产。
- 一个数字货币样例资产。
- 一个混合候选池。
- A 股日线和数字货币 1h K 线。

该脚本可以重复运行，用于验证 Repository 的幂等写入和基础查询。

也可以运行真实数据源到数据库的冒烟脚本：

```bash
python scripts/data/smoke_providers.py
```

该脚本会尝试读取 AKShare A 股日线和 ccxt Binance K 线。公网接口可能因为网络、SSL 或数据源限流失败；失败时 Provider 应返回结构化 `error`，不能让异常穿透到推荐链路。

## 5. 设计约束

- 全环境必须使用 PostgreSQL + TimescaleDB。
- 不使用 SQLite 作为应用运行环境或测试环境。
- `market_bars` 是 TimescaleDB hypertable，唯一约束必须包含 `timestamp`。
- 指标、因子、信号和评分保存的是推荐链路快照，不是实时指标流水。
