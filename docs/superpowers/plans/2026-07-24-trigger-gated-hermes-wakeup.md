# 触发器门控 Hermes 唤醒实施计划

## 目标

让 finance-agent 先用确定性程序规则评估已入库事实，只有发生超过阈值且通过去重、冷却和数据质量校验的事件时，才唤醒 Hermes 模型。无事件路径不产生模型调用；Webhook 失败时事件不丢失并可重试；内部 Agent 与 Hermes 不消费同一事件。

## 现状与风险

- `TriggerService.evaluate()` 已经是纯程序化规则，不需要模型。
- 当前 `dispatch_pending()` 会直接把 pending 事件标记为 dispatched，未确认外部唤醒成功。
- 内部 Agent 消费入口没有强制按 `agent_runtime` 隔离，可能抢走 Hermes 事件。
- Hermes 当前普通 Cron 每次先启动模型，再调用 `run_triggers_once`，无事件也会消耗模型调用。
- GitNexus 索引落后且 impact 查询触发 WAL/FTS `UNREACHABLE_CODE`，本计划以源码调用链和测试作为降级依据，并在完成前运行 `detect_changes`；不修改无法确认的共享数据模型字段。

## 实施步骤

1. 先写红灯测试，覆盖 Webhook 成功/失败、重复事件、cooldown、运行时隔离和空路径。
2. 增加受控 Webhook 发布器，使用 HMAC、超时和退避；只有 HTTP 成功后才确认事件已派发。
3. 将触发评估调度器配置为唯一事件生产者，Hermes 事件默认运行时为 `hermes_agent`，内部 Agent 只消费 `internal_agent_loop`。
4. 为 Hermes Gateway 启用静态受控 Webhook 路由；删除普通模型 Cron，改为无模型 watchdog 或由 finance-agent 发布器直接唤醒。
5. 更新 Docker/环境变量，提供 Webhook 地址、密钥和发布超时配置；密钥只从环境变量读取，不写入仓库。
6. 运行单元测试、Webhook 模拟集成测试、调度器回归测试和 Hermes 运行态检查，确认安静路径没有模型调用。

## 验收标准

- 没有超过阈值的事件时，触发检查正常完成且 Hermes 模型调用数为 0。
- 同一 `dedup_key` 在 cooldown 内只产生一次唤醒。
- Webhook 2xx 后事件为 `dispatched`；网络失败后仍可重试且不会伪装成成功。
- 内部 Agent 不读取 `hermes_agent` 事件，Hermes 不读取 `internal_agent_loop` 事件。
- 触发事件、Webhook 请求、重试和最终处理状态均可审计。
- 不新增下单、撤单或券商写接口。
