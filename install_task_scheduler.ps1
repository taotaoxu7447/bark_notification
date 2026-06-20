$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Label = "CodexWatchNotifier"
$ConfigDir = Join-Path $env:USERPROFILE ".codex-watch-notifier"
$EnvFile = Join-Path $ConfigDir "env"
$RuntimeDir = Join-Path $ConfigDir "bin"
$RunScript = Join-Path $RuntimeDir "run_notifier.ps1"

New-Item -ItemType Directory -Force -Path $ConfigDir, $RuntimeDir | Out-Null

if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $ScriptDir "env.example") $EnvFile
    Write-Host "Created $EnvFile"
    Write-Host "Edit it and set BARK_URL or BARK_KEY, then run this installer again."
    exit 0
}

Copy-Item (Join-Path $ScriptDir "codex_watch_notifier.py") (Join-Path $RuntimeDir "codex_watch_notifier.py") -Force

@"
`$ErrorActionPreference = "Stop"
`$RuntimeDir = "$RuntimeDir"
`$Script = Join-Path `$RuntimeDir "codex_watch_notifier.py"
`$OutLog = Join-Path "$ConfigDir" "task.out.log"
`$ErrLog = Join-Path "$ConfigDir" "task.err.log"
`$Py = Get-Command py -ErrorAction SilentlyContinue
if (`$Py) {
    & `$Py.Source -3 `$Script >> `$OutLog 2>> `$ErrLog
} else {
    `$Python = Get-Command python -ErrorAction Stop
    & `$Python.Source `$Script >> `$OutLog 2>> `$ErrLog
}
"@ | Set-Content -Encoding UTF8 $RunScript

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel LeastPrivilege
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $Label -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $Label

Write-Host "Installed and started scheduled task $Label"
Write-Host "Config: $EnvFile"
Write-Host "Logs: $(Join-Path $ConfigDir "task.out.log") and $(Join-Path $ConfigDir "task.err.log")"
Write-Host "Run: py -3 $(Join-Path $RuntimeDir "codex_watch_notifier.py") --doctor"
