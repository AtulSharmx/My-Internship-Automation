# Builds a clean archive to upload to the server.
# Leaves out the Windows venv, the local browser profile (cookies cannot be
# moved between machines anyway) and the log/lock files.
#
#   powershell -ExecutionPolicy Bypass -File deploy\make_bundle.ps1
#
# Result: wfbot-deploy.tar.gz in the project root.
$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$out = Join-Path $root "wfbot-deploy.tar.gz"
if (Test-Path $out) { Remove-Item $out -Force }

Push-Location $root
try {
    tar -czf $out `
        --exclude=.venv `
        --exclude=browser_profile `
        --exclude=logs `
        --exclude=__pycache__ `
        --exclude=.git `
        --exclude=data/wfbot.lock `
        --exclude=wfbot-deploy.tar.gz `
        .
}
finally {
    Pop-Location
}

$mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "Created $out ($mb MB)"
Write-Host "Upload with:  scp `"$out`" root@SERVER_IP:/opt/"
