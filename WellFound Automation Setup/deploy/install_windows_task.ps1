# ---------------------------------------------------------------------------
# Registers the discovery agent so it starts by itself at every Windows logon.
#
# After this has run once you never have to open a terminal again: switch the
# laptop on, sign in, and the Telegram agent comes up on its own.
#
#   powershell -ExecutionPolicy Bypass -File deploy\install_windows_task.ps1
#
# The task runs .venv\Scripts\pythonw.exe watchdog.py.  pythonw.exe has no
# console, so nothing flashes on screen and no window is left behind.
# ---------------------------------------------------------------------------
$ErrorActionPreference = "Stop"

$TaskName = "WellFound Intern Bot"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$watchdog = Join-Path $root "watchdog.py"

if (-not (Test-Path $pythonw)) {
    throw "pythonw.exe not found at $pythonw - run setup.ps1 first."
}
if (-not (Test-Path $watchdog)) {
    throw "watchdog.py not found at $watchdog"
}

# A stale "switched off" marker would keep the agent from starting.
$stopFile = Join-Path $root "data\stop_agent"
if (Test-Path $stopFile) {
    Remove-Item $stopFile -Force
    Write-Host "Cleared the 'switched off' marker."
}

# The venv interpreter re-executes the real worker as a child process, so
# stopping the task can leave that worker behind.  Clear out anything that is
# still running from this project before the task is registered again.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$root*" }
foreach ($proc in $running) {
    Write-Host "Stopping a leftover process (pid $($proc.ProcessId))."
    & cmd /c "taskkill /PID $($proc.ProcessId) /T /F >nul 2>&1"
}
if ($running) {
    Start-Sleep -Seconds 2
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "`"$watchdog`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Safety net: if the task is ever stopped by hand (or a Windows update ends
# it), it comes back within a quarter of an hour.  "IgnoreNew" in the task
# settings means this trigger does nothing while the task is already running.
# No repetition duration is set on purpose, so it repeats indefinitely.
$repeat = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(15) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -Hidden `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger, $repeat `
    -Settings $settings `
    -Principal $principal `
    -Description "Runs the Wellfound discovery agent at every logon, silently, and restarts it after a crash." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "Task '$TaskName' is installed."
Write-Host "  state        : $($task.State)"
Write-Host "  last result  : $($info.LastTaskResult)"
Write-Host "  project root : $root"
Write-Host ""
Write-Host "The agent now starts by itself whenever you sign in to Windows."
Write-Host "Check it with:  .venv\Scripts\python.exe watchdog.py status"
Write-Host "Switch it off:  .venv\Scripts\python.exe watchdog.py stop"
