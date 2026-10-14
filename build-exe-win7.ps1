$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $projectDir ".build-venv-win7"
$pythonPath = Join-Path $venvDir "Scripts\python.exe"
$requirementsPath = Join-Path $projectDir "requirements-build-win7.txt"
$specPath = Join-Path $projectDir "rpa-flow-studio.spec"
$pipIndexUrl = if ($env:RPA_PIP_INDEX_URL) { $env:RPA_PIP_INDEX_URL } else { "https://pypi.tuna.tsinghua.edu.cn/simple" }
$uv = Get-Command "uv.exe" -ErrorAction SilentlyContinue

if ($null -eq $uv) { throw "uv was not found. Install uv before building the Windows 7 package." }

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host "Creating the Python 3.8 Windows 7 build environment..." -ForegroundColor Cyan
    & $uv.Source venv --python 3.8 $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath)) { throw "Could not create the Python 3.8 build environment." }
}

Push-Location -LiteralPath $projectDir
try {
    Write-Host "Installing Windows 7 compatible dependencies..." -ForegroundColor Cyan
    & $uv.Source pip install --python $pythonPath --index-url $pipIndexUrl -r $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw "Windows 7 dependency installation failed." }

    Write-Host "Building the Windows 7 compatible EXE..." -ForegroundColor Cyan
    $env:RPA_EXE_NAME = "RPA-Flow-Studio-Win7-v6"
    & $pythonPath -m PyInstaller --noconfirm --clean $specPath
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller Windows 7 build failed." }

    $exePath = Join-Path $projectDir "dist\RPA-Flow-Studio-Win7-v6.exe"
    if (-not (Test-Path -LiteralPath $exePath)) { throw "No EXE was found in the dist directory." }
    Write-Host "Build completed: $exePath" -ForegroundColor Green
}
finally { Pop-Location }
