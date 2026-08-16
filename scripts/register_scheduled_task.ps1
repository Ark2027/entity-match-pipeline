param(
    [string]$TaskName = 'Entity Match Pipeline - Quarterly',
    [string]$NotificationTaskName = 'Entity Match Pipeline - Logon Reminder',
    [string]$SafetyTaskName = 'Entity Match Pipeline - Catch-up',
    [string]$StartTime = '09:00',
    [string]$SafetyStartTime = '10:00'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $projectRoot 'scripts\run_quarterly.ps1'

$quarterlyCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" --disable-notifications --queue-logon-notification --deliver-pending-notification"
$logonCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" --ensure-due-quarter-run --disable-notifications --queue-logon-notification --deliver-pending-notification"
$safetyCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" --ensure-due-quarter-run --disable-notifications --queue-logon-notification --deliver-pending-notification"

schtasks.exe /Create `
    /TN $TaskName `
    /TR $quarterlyCommand `
    /SC MONTHLY `
    /M JAN,APR,JUL,OCT `
    /D 30 `
    /ST $StartTime `
    /F | Out-Null

schtasks.exe /Create `
    /TN $NotificationTaskName `
    /TR $logonCommand `
    /SC ONLOGON `
    /F | Out-Null

schtasks.exe /Create `
    /TN $SafetyTaskName `
    /TR $safetyCommand `
    /SC DAILY `
    /ST $SafetyStartTime `
    /RI 120 `
    /DU 12:00 `
    /F | Out-Null

Write-Host "Registered '$TaskName' for Jan/Apr/Jul/Oct 30 at $StartTime."
Write-Host "Registered '$NotificationTaskName' to catch up missed due runs and notify you at logon."
Write-Host "Registered '$SafetyTaskName' to re-check for missed due runs every 2 hours while you're logged in."
