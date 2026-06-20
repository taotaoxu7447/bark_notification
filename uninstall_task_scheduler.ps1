$ErrorActionPreference = "Stop"

$Label = "CodexWatchNotifier"

Unregister-ScheduledTask -TaskName $Label -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "Stopped and removed scheduled task $Label"
Write-Host "Config/logs under $env:USERPROFILE\.codex-watch-notifier were left in place."
