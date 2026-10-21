# ---------------------------------------------------------------------------
# Controls the discovery agent from PowerShell.
#
#   powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 status
#   powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 start
#   powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 stop
#   powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 restart
#   powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 install
#   powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 uninstall
# ---------------------------------------------------------------------------
param(
    [Parameter(Position = 0)]
    [ValidateSet("status", "start", "stop", "restart", "install", "uninstall")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$TaskName = "WellFound Intern Bot"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"

function Invoke-Agent([string[]]$Arguments) {
    & $python (Join-Path $root "watchdog.py") @Arguments
}

switch ($Action) {
    "status"   { Invoke-Agent @("status") }
    "start"    { Invoke-Agent @("start") }
    "stop"     { Invoke-Agent @("stop") }
    "restart"  { Invoke-Agent @("restart") }
    "install" {
        & (Join-Path $PSScriptRoot "install_windows_task.ps1")
    }
    "uninstall" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        $root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
        $running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*$root*" }
        foreach ($proc in $running) {
            & cmd /c "taskkill /PID $($proc.ProcessId) /T /F >nul 2>&1"
        }
        if ($null -eq $task) {
            Write-Host "Task '$TaskName' is not installed."
            break
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Task '$TaskName' has been removed."
    }
}
