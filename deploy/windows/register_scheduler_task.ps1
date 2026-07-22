#requires -Version 5.1
<#
Windows 本地基础数据调度器已废弃。

调度器统一由根目录 docker-compose.yml 中的
finance-agent-scheduler 服务运行。本脚本保留为兼容入口，防止旧部署文档
再次注册第二个本地调度器。
##>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "FinanceAgent-BaseDataScheduler"
)

$ErrorActionPreference = "Stop"
$message = @"
Windows 本地 scheduler 已禁用：$TaskName
请使用 Docker Compose 启动统一调度器：
  docker compose up -d --build finance-agent-gotdx-gateway finance-agent-scheduler
如需清理旧的 Windows 计划任务，请执行：
  powershell -File deploy\windows\unregister_tasks.ps1
"@

if ($PSCmdlet.ShouldProcess($TaskName, "Register deprecated Windows scheduler")) {
    throw $message
}

Write-Warning $message
