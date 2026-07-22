# Windows 部署辅助工具

Windows 端只负责运行本地 API；基础数据调度器和 gotdx 网关统一由项目根目录的 Docker Compose 管理。本目录不再注册 Windows 本地 scheduler。

## 安装

先在项目根目录完成迁移和前端构建：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
cd apps\agent-office
npm run build
cd ..\..
```

启动统一的 Docker 服务：

```powershell
docker compose up -d --build finance-agent-gotdx-gateway finance-agent-scheduler
```

注册本地 API（可选）：

```powershell
powershell -File deploy\windows\register_api_task.ps1 -WhatIf
powershell -File deploy\windows\register_api_task.ps1
```

当前 API 无认证层，默认只绑定 `127.0.0.1:8000`，不要直接暴露到局域网或公网。

`register_scheduler_task.ps1` 已改为拒绝注册的兼容入口。若存在历史 Windows 调度任务，请执行：

```powershell
powershell -File deploy\windows\unregister_tasks.ps1 -WhatIf
powershell -File deploy\windows\unregister_tasks.ps1
```

## 查看状态

```powershell
docker compose ps finance-agent-gotdx-gateway finance-agent-scheduler
docker compose logs --since 10m finance-agent-scheduler
```

API 状态接口：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/data/scheduler/status
```

## 日志位置

- 状态文件：`runtime/base_data_scheduler/status.json`
- 事件日志：`runtime/base_data_scheduler/events.jsonl`
- Docker 调度器日志：`docker compose logs finance-agent-scheduler`

`status.json` 和 `events.jsonl` 由 Docker scheduler 写入，Web API 只读取它们，不再维护宿主机 PID 元数据。

## 升级流程

1. 更新代码并执行迁移：

   ```powershell
   git pull
   .\.venv\Scripts\python.exe -m alembic upgrade head
   ```

2. 重新构建并重启 Docker 服务：

   ```powershell
   docker compose up -d --build finance-agent-gotdx-gateway finance-agent-scheduler
   ```

3. 如 API 也需要更新，重启 `FinanceAgent-Api` 计划任务。

## gotdx 网关

gotdx 网关运行在 `finance-agent-gotdx-gateway` 容器内，scheduler 使用 Compose 服务名 `http://finance-agent-gotdx-gateway:8790` 访问。不要在 scheduler 容器中配置 `127.0.0.1:8790` 或宿主机地址。
