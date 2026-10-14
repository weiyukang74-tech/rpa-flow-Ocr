$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $projectDir ".build-venv"
$pythonPath = Join-Path $venvDir "Scripts\python.exe"
$requirementsPath = Join-Path $projectDir "requirements-build.txt"
$specPath = Join-Path $projectDir "rpa-flow-studio.spec"
$pipIndexUrl = if ($env:RPA_PIP_INDEX_URL) {
    $env:RPA_PIP_INDEX_URL
}
else {
    "https://pypi.tuna.tsinghua.edu.cn/simple"
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -eq $pyLauncher) {
        throw "Python launcher py.exe was not found. Install 64-bit Python 3.10 first."
    }
    Write-Host "Creating the Python 3.10 build environment..." -ForegroundColor Cyan
    & $pyLauncher.Source -3.10 -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath)) {
        throw "Could not create the Python 3.10 build environment."
    }
}

Push-Location -LiteralPath $projectDir
try {
    Write-Host "Installing or updating build dependencies..." -ForegroundColor Cyan
    & $pythonPath -m pip install --disable-pip-version-check --index-url $pipIndexUrl --timeout 120 --retries 3 -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. Check the network or RPA_PIP_INDEX_URL."
    }

    Write-Host "Building the single-file EXE. This may take several minutes..." -ForegroundColor Cyan
    & $pythonPath -m PyInstaller --noconfirm --clean $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }

    $exePath = Get-ChildItem -LiteralPath (Join-Path $projectDir "dist") -Filter "*.exe" -File | Select-Object -First 1
    if ($null -eq $exePath) {
        throw "The build completed but no EXE was found in the dist directory."
    }
    Write-Host "Build completed: $($exePath.FullName)" -ForegroundColor Green
}
finally {
    Pop-Location
}
