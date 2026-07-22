# gotdx 行情网关 PoC

这是一个隔离的只读 PoC，不会修改 `finance-agent` 当前的 Python 调度器、数据库或推荐链路。

## 启动

```powershell
go run .
```

默认只监听 `127.0.0.1:8790`。可通过以下环境变量调整：

- `TDX_GATEWAY_ADDR`：监听地址，默认 `127.0.0.1:8790`；
- `TDX_TIMEOUT_SECONDS`：通达信连接/读写超时，默认 3 秒。

## 接口

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8790/healthz
```

重点标的快照：

```powershell
$body = @{ symbols = @("600519.SH", "000001.SZ") } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8790/quotes `
  -ContentType "application/json" -Body $body
```

异动候选：

```powershell
$body = @{ market = "SH"; start = 0; count = 100 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8790/unusual `
  -ContentType "application/json" -Body $body
```

## 质量门禁

- `quality_status=available` 只表示交易窗口内服务端时间与本机时间差不超过 10 秒；
- 周末、收盘后、午休或服务端时间落后时返回 `after_hours_snapshot` 或 `stale`，不能直接生成卖出信号；
- 首次请求可能触发通达信节点测速，生产接入前应预热连接并单独统计冷启动延迟；
- 网关只支持沪深北 A 股代码，不提供自动交易能力。
