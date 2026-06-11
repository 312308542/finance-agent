# GitNexus 差异记录

> 用途：开发过程中如 GitNexus 索引落后、MCP 不可用或只能使用 CLI fallback，在此记录差异和处理方式。全部开发完成后由总负责人统一执行 `gitnexus analyze` 更新索引。

| 日期 | 当前任务 | 差异项 | 处理方式 |
| --- | --- | --- | --- |
| 2026-06-12 | 批次 1 / 02-T1 | 总负责人已重新执行 `gitnexus analyze`，当前 `Indexed commit` 与 `Current commit` 均为 `d028988`，暂无索引差异 | 后续如提交后索引落后，不再中断开发；记录差异并在全部开发完成后统一更新 |
| 2026-06-12 | 批次 1 / 02-T2 | `C:\Users\Administrator\.codex\config.toml` 已配置 `[mcp_servers.gitnexus]`，但当前 Codex 会话可调用工具列表未暴露 GitNexus MCP namespace，`tool_search` 未检索到 `gitnexus` 工具，`list_mcp_resources` 返回空，无法在编辑前通过 MCP 执行 `gitnexus_impact` | 不使用 `npx` 或 CLI 代替 MCP；沿用前序已完成的 `export_scheduler_payload` CRITICAL 影响分析结论，保持 `sync_config.py` 纯追加改动，并在全部开发完成后由总负责人统一更新索引 |
| 2026-06-12 | 批次 1 / 02-T3/T4 | 修改 `BaseDataScheduler.__init__`、`run_job`、`execute_job`、新增 `build_trigger_evaluation_kwargs`、`build_agent_loop_consume_kwargs`、`run_trigger_evaluation`、`run_agent_loop_consume` 前，GitNexus MCP namespace 仍未暴露，无法通过 MCP 执行 `gitnexus_impact` / `gitnexus_detect_changes` | 使用 PyCharm MCP 和既有测试定位影响面；改动保持为调度器新增 job_type 分支和可注入执行器，不改已有 collection/recommendation/data_quality/technical_screening 分支；相关回归 `93 passed`，全部开发完成后由总负责人统一更新 GitNexus 索引 |
| 2026-06-12 | 批次 1 / 02-T5 | 修改 `TriggerEvaluationRequest`、`TriggerEvaluationResult`、`TriggerService.evaluate`，新增 `_evaluate_intraday_volatility_triggers`、`_load_recent_realtime_quote_snapshots`、`trigger_group_enabled` 等盘中规则辅助函数前，GitNexus MCP namespace 仍未暴露，无法执行 `gitnexus_impact` | 使用 PyCharm MCP 定位 `TriggerService`，按方案限定只读取入库 `realtime_quote_snapshots`，不改既有持仓/观察池/推荐/风险/质量规则语义；新增 TDD 测试 `tests/test_intraday_trigger_rules.py`，相关验证 `3 passed` + 调度回归 `93 passed` |
| 2026-06-12 | 批次 1 / 02-T6 | 修改 `JOB_TYPES`、`TRADING_DAY_POLICIES` 和 `as_job_group_choice` 前，GitNexus MCP namespace 仍未暴露，无法执行 `gitnexus_impact` / `gitnexus_detect_changes` | 由 runtime 解析红灯暴露缺口，最小扩展调度配置白名单；`runtime/` 为本地忽略文件，已重新导出但不提交；验证 `parse_scheduler_config` 可解析 34 个 jobs，相关 pytest `97 passed` |
| 2026-06-12 | 批次 1 / 02-T7/T8 | `C:\Users\Administrator\.codex\config.toml` 中确认存在 `[mcp_servers.gitnexus]`，但当前 Codex 会话工具清单仍未暴露 GitNexus MCP namespace；`tool_search` 未检索到 GitNexus 工具，`list_mcp_resources` 返回空 | T7/T8 仅做真实冒烟与文档同步，不新增业务符号；已记录 `--only` 参数与当前脚本不一致的验收差异，后续全部开发完成后由总负责人统一执行 GitNexus 更新和影响复核 |
