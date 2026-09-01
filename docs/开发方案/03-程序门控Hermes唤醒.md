# 程序门控 Hermes 唤醒

## 目的

触发器必须先由 finance-agent 使用已入库事实和确定性阈值判断异常，只有事件通过阈值、数据质量、去重和冷却后才唤醒 Hermes。没有事件时不启动模型，避免盘中轮询造成无效 Token 消耗。

## 链路

```text
行情/指标入库
  -> TriggerService.evaluate（纯程序）
  -> dedup/cooldown/数据闸门
  -> HermesWebhookPublisher（HMAC）
  -> Hermes Webhook /webhooks/finance-agent
  -> 联网、Finance Memory、图谱和 Workflow 复核
  -> 微信通知，交易动作仍需用户确认
```

`analytics.triggers.evaluate.daily` 只在收盘最终 K 线完成后运行一次，`analytics.triggers.evaluate.intraday` 仅在 A 股交易时段按 15 分钟运行。默认调度计划不再自动执行 `agent_loop_consume`，避免内部 Agent 抢占 Hermes 事件。

## 事件状态

- `pending`：程序已发现事件，等待 Webhook 成功。
- `dispatched`：Webhook 返回 2xx，Hermes 已收到唤醒请求。
- `skipped`：缺少 Workflow 所需上下文，事件被明确跳过。
- 派发失败仍保持 `pending`，payload 写入错误、失败时间和 `dispatch_retry_at`，下一轮到期后重试。

内部 Agent 若被单独启用，只能查询 `agent_runtime=internal_agent_loop`；Hermes 事件使用 `agent_runtime=hermes_agent`，两者不会竞争同一事件。

## 配置

finance-agent 读取：

- `FINANCE_AGENT_HERMES_WEBHOOK_URL`
- `FINANCE_AGENT_HERMES_WEBHOOK_SECRET`
- `FINANCE_AGENT_HERMES_WEBHOOK_TIMEOUT_SECONDS`

Hermes Gateway 使用 `/root/.hermes/config.yaml` 与动态订阅 `finance-agent`，签名头为 `X-Hub-Signature-256`。密钥只保存在本机环境，不提交到仓库。

当前本机 Docker 因镜像加速源无法拉取 gotdx 的 Alpine/Go 基础镜像，scheduler compose 服务临时使用 `./src:/app/src:ro` 和 `PYTHONPATH=/app/src` 直接加载工作区源码；恢复镜像源后应重新构建 scheduler 镜像，并可移除该源码挂载。

## 验收

1. 无事件时调度任务成功完成且没有 Hermes Webhook 请求。
2. 有事件时同一 `dedup_key` 在冷却窗口内只发送一次。
3. Webhook 返回非 2xx 时事件仍为 `pending`，可在退避时间后重试。
4. Hermes 输出必须引用确定性事实、最新联网来源、Memory/图谱证据和数据闸门；不得下单或撤单。
