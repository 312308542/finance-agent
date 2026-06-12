# Windows 常驻部署模板

本目录提供 finance-agent 在 Windows 单机环境下的计划任务模板，用于开机后自动启动基础数据调度器和本地 API 服务。

## 安装

先在项目根目录完成依赖、迁移和前端构建：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
cd apps\agent-office
npm run build
cd ..\..
```

注册基础数据调度器：

```powershell
powershell -File deploy\windows\register_scheduler_task.ps1 -WhatIf
powershell -File deploy\windows\register_scheduler_task.ps1
```

注册本地 API：

```powershell
powershell -File deploy\windows\register_api_task.ps1 -WhatIf
powershell -File deploy\windows\register_api_task.ps1
```

默认 API 只绑定 `127.0.0.1:8000`。当前 API 无认证层，不要直接暴露到局域网或公网；如需远程访问，应先增加反向代理、认证、TLS 和访问审计。

## 查看状态

```powershell
Get-ScheduledTask -TaskName FinanceAgent-BaseDataScheduler,FinanceAgent-Api
Get-ScheduledTaskInfo -TaskName FinanceAgent-BaseDataScheduler
Get-ScheduledTaskInfo -TaskName FinanceAgent-Api
```

调度器健康检查：

```powershell
.\.venv\Scripts\python.exe scripts\data\run_base_data_scheduler.py --health-check --status-file runtime\base_data_scheduler\status.json
```

## 日志位置

- 状态文件：`runtime/base_data_scheduler/status.json`
- 事件日志：`runtime/base_data_scheduler/events.jsonl`
- API 输出：Windows 计划任务历史或后续接入的日志代理。

`status.json` 用于判断常驻调度器心跳，`events.jsonl` 用于排查单个任务开始、成功、失败和重试。

## 升级流程

1. 停止计划任务：

   ```powershell
   Stop-ScheduledTask -TaskName FinanceAgent-BaseDataScheduler
   Stop-ScheduledTask -TaskName FinanceAgent-Api
   ```

2. 更新代码并执行迁移：

   ```powershell
   git pull
   .\.venv\Scripts\python.exe -m alembic upgrade head
   ```

3. 重新构建前端：

   ```powershell
   cd apps\agent-office
   npm run build
   cd ..\..
   ```

4. 启动计划任务：

   ```powershell
   Start-ScheduledTask -TaskName FinanceAgent-Api
   Start-ScheduledTask -TaskName FinanceAgent-BaseDataScheduler
   ```

## 卸载

```powershell
powershell -File deploy\windows\unregister_tasks.ps1 -WhatIf
powershell -File deploy\windows\unregister_tasks.ps1
```

卸载只移除计划任务，不删除数据库、`runtime/` 数据或本地配置。
