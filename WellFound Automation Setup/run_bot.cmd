@echo off
rem ---------------------------------------------------------------------------
rem Starts the Wellfound discovery agent through the silent supervisor.
rem
rem The scheduled task "WellFound Intern Bot" starts watchdog.py directly
rem (with pythonw.exe, so no window appears).  This file is the manual
rem equivalent: double-click it, or run it from a terminal, to start or stop
rem the agent by hand.
rem
rem   run_bot.cmd            start the agent (visible console, shows the log)
rem   run_bot.cmd status     report what is running
rem   run_bot.cmd stop       switch the agent off
rem   run_bot.cmd start      switch the agent back on
rem
rem Everything is relative to this file, so the project folder can be moved
rem anywhere without breaking the autostart.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo The virtual environment is missing. Run setup.ps1 once first.
    exit /b 1
)

if /i "%~1"=="status" (
    ".venv\Scripts\python.exe" "watchdog.py" status
    exit /b %ERRORLEVEL%
)
if /i "%~1"=="stop" (
    ".venv\Scripts\python.exe" "watchdog.py" stop
    exit /b %ERRORLEVEL%
)
if /i "%~1"=="start" (
    ".venv\Scripts\python.exe" "watchdog.py" start
    exit /b %ERRORLEVEL%
)
if /i "%~1"=="restart" (
    ".venv\Scripts\python.exe" "watchdog.py" restart
    exit /b %ERRORLEVEL%
)

".venv\Scripts\python.exe" "watchdog.py" run
endlocal
