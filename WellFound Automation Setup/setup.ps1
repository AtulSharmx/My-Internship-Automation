# One-shot Windows setup for the Wellfound Startup Discovery Agent.
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Creates .venv and installs the runtime dependencies.  The manual browser
# helper and the preserved legacy internship bot also use Playwright; its
# Chromium download is attempted but is not required for normal imports,
# exports, or permitted URL discovery.
#
# Next:
#   .\.venv\Scripts\python.exe setup_discovery.py
#   .\.venv\Scripts\python.exe -m startup_agent.cli init
#   .\.venv\Scripts\python.exe run.py

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Find-Python {
    foreach ($candidate in @("python", "py")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            return $candidate
        }
    }
    throw "Python was not found on PATH. Install Python 3.11+ from python.org first."
}

$python = Find-Python
Write-Host "==> Using $($python)"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "==> Creating virtual environment"
    & $python -m venv .venv
}

$vp = ".venv\Scripts\python.exe"

Write-Host "==> Installing dependencies"
& $vp -m pip install --upgrade pip | Out-Null
& $vp -m pip install -r requirements.txt

Write-Host "==> Installing Playwright's Chromium for the optional manual browser helper"
try {
    & $vp -m playwright install chromium
} catch {
    Write-Warning "Playwright Chromium download failed; normal discovery/import/export still works."
}

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  $vp setup_discovery.py                  # Telegram + target range + permitted sources"
Write-Host "  $vp -m startup_agent.cli init           # create data/import/export folders"
Write-Host "  $vp -m startup_agent.cli import imports # import any files you placed there"
Write-Host "  $vp run.py                              # start the Telegram agent"
