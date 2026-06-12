#requires -Version 5.1
<#
注册 finance-agent 基础数据调度器 Windows 计划任务。

默认以当前登录用户权限运行，适合单机开发/个人生产环境。
如需部署到服务器或局域网，请先补充系统级账号、日志轮转和访问控制方案。
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot = "",
    [string]$TaskName = "FinanceAgent-BaseDataScheduler",
    [string]$PythonExe,
    [string]$ConfigFile,
    [string]$StatusFile,
    [string]$EventLogFile,
    [string]$UserId = $env:USERNAME,
    [ValidateSet("Limited", "Highest")]
    [string]$RunLevel = "Limited"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if ([string]::IsNullOrWhiteSpace($ConfigFile)) {
    $ConfigFile = Join-Path $ProjectRoot "runtime\base_data_scheduler\base_data_scheduler.json"
}
if ([string]::IsNullOrWhiteSpace($StatusFile)) {
    $StatusFile = Join-Path $ProjectRoot "runtime\base_data_scheduler\status.json"
}
if ([string]::IsNullOrWhiteSpace($EventLogFile)) {
    $EventLogFile = Join-Path $ProjectRoot "runtime\base_data_scheduler\events.jsonl"
}

$runtimeDir = Split-Path -Parent $StatusFile
if (-not (Test-Path -LiteralPath $runtimeDir)) {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable not found: $PythonExe. Please create project .venv first."
}

$arguments = @(
    "scripts\data\run_base_data_scheduler.py",
    "--config", "`"$ConfigFile`"",
    "--loop",
    "--status-file", "`"$StatusFile`"",
    "--event-log-file", "`"$EventLogFile`""
) -join " "

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $arguments `
    -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Days 30)
$principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType Interactive `
    -RunLevel $RunLevel

if ($PSCmdlet.ShouldProcess($TaskName, "Register finance-agent scheduler task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "finance-agent base data scheduler daemon" `
        -Force | Out-Null
    Write-Host "Registered scheduled task: $TaskName"
    Write-Host "Status file: $StatusFile"
    Write-Host "Event log file: $EventLogFile"
}
