#requires -Version 5.1
<#
卸载 finance-agent Windows 计划任务。
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string[]]$TaskNames = @(
        "FinanceAgent-BaseDataScheduler",
        "FinanceAgent-Api"
    )
)

$ErrorActionPreference = "Stop"

foreach ($taskName in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Scheduled task not found, skipped: $taskName"
        continue
    }
    if ($PSCmdlet.ShouldProcess($taskName, "Unregister scheduled task")) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Unregistered scheduled task: $taskName"
    }
}
