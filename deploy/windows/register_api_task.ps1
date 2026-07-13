#requires -Version 5.1
<#
注册 finance-agent API Windows 计划任务。

安全边界：当前 API 无认证层，默认只绑定 127.0.0.1。
如需局域网访问，请先增加反向代理、认证和访问审计，不要直接暴露 FastAPI 端口。
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot = "",
    [string]$TaskName = "FinanceAgent-Api",
    [string]$PythonExe,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
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
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable not found: $PythonExe. Please create project .venv first."
}
if ($BindHost -ne "127.0.0.1" -and $BindHost -ne "localhost") {
    Write-Warning "The current API has no authentication. Add reverse proxy, auth, and access control before binding $BindHost."
}

$arguments = @(
    "-m uvicorn finance_agent.api.app:app",
    "--app-dir src",
    "--host $BindHost",
    "--port $Port"
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

if ($PSCmdlet.ShouldProcess($TaskName, "Register finance-agent API task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "finance-agent local API service" `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "Registered scheduled task: $TaskName"
    Write-Host "API URL: http://$BindHost`:$Port"
}
