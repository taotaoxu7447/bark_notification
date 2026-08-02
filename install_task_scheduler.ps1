$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cli = Join-Path $ScriptDir "agentwatch.py"
$Py = Get-Command py -ErrorAction SilentlyContinue

if ($Py) {
    & $Py.Source -3 $Cli install @args
} else {
    $Python = Get-Command python -ErrorAction Stop
    & $Python.Source $Cli install @args
}
exit $LASTEXITCODE
