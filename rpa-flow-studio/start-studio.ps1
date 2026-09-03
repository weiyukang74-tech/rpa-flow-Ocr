$ErrorActionPreference = "Stop"

$studioDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$defaultPython = Join-Path $studioDir "..\..\..\RPA_his\.venv-rpa312\Scripts\python.exe"
$pythonPath = if ($env:RPA_PYTHON) { $env:RPA_PYTHON } else { $defaultPython }

try {
    $resolvedPython = (Resolve-Path -LiteralPath $pythonPath).Path
    Push-Location -LiteralPath $studioDir
    try {
        & $resolvedPython (Join-Path $studioDir "server.py")
        exit $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Host "Flow Configurator startup error:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
