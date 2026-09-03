$ErrorActionPreference = "Stop"

$studioDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $studioDir ".venv"
$localPython = Join-Path $venvDir "Scripts\python.exe"
$requirements = Join-Path $studioDir "requirements.txt"
$wheelhouse = Join-Path $studioDir "wheelhouse"
$defaultPipIndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple"
$pipIndexUrl = if ($env:RPA_PIP_INDEX_URL) {
    $env:RPA_PIP_INDEX_URL
}
else {
    $defaultPipIndexUrl
}

function Test-CompatiblePython {
    param(
        [string]$Executable,
        [string[]]$PrefixArguments = @()
    )

    $previousErrorAction = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 turns redirected native stderr into an
        # ErrorRecord. Probe runtimes without letting a missing version abort
        # the whole launcher.
        $ErrorActionPreference = "SilentlyContinue"
        & $Executable @PrefixArguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Find-BasePython {
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher -and
        (Test-CompatiblePython -Executable $pyLauncher.Source -PrefixArguments @("-3"))) {
        return @($pyLauncher.Source, "-3")
    }

    $pythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand -and
        (Test-CompatiblePython -Executable $pythonCommand.Source)) {
        return @($pythonCommand.Source)
    }
    return @()
}

function Test-ProjectDependencies {
    param([string]$PythonPath)

    $previousErrorAction = $ErrorActionPreference
    try {
        # A missing import writes a traceback to native stderr. Windows
        # PowerShell 5.1 must treat that as a normal false probe so the
        # installer can repair an incomplete environment.
        $ErrorActionPreference = "SilentlyContinue"
        & $PythonPath -c "import mss, PIL, pywinauto, rapidocr, onnxruntime" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Test-PipAvailable {
    param([string]$PythonPath)

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $PythonPath -m pip --version 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

function Ensure-Pip {
    param([string]$PythonPath)

    if (Test-PipAvailable -PythonPath $PythonPath) {
        return
    }

    Write-Host "pip is missing. Bootstrapping pip with ensurepip..." -ForegroundColor Cyan
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $PythonPath -m ensurepip --upgrade --default-pip
        $ensurePipExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($ensurePipExitCode -ne 0 -or -not (Test-PipAvailable -PythonPath $PythonPath)) {
        throw "Failed to bootstrap pip in the project Python environment."
    }
}

function Install-ProjectDependencies {
    param([string]$PythonPath)

    if (-not (Test-Path -LiteralPath $requirements)) {
        throw "Missing dependency list: $requirements"
    }

    Ensure-Pip -PythonPath $PythonPath
    Write-Host "Installing Flow Configurator dependencies. Keep the network available..." -ForegroundColor Cyan
    if ((Test-Path -LiteralPath $wheelhouse) -and
        (Get-ChildItem -LiteralPath $wheelhouse -File -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        & $PythonPath -m pip install --disable-pip-version-check --no-index --find-links $wheelhouse -r $requirements
    }
    else {
        Write-Host "Using Python package index: $pipIndexUrl" -ForegroundColor DarkGray
        & $PythonPath -m pip install --disable-pip-version-check --index-url $pipIndexUrl --timeout 60 --retries 3 -r $requirements
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed. Check the network, proxy, or wheelhouse folder."
    }
}

try {
    if ($env:RPA_SETUP_CHECK -eq "1") {
        $detectedPython = @(Find-BasePython)
        if ($detectedPython.Count -eq 0) {
            throw "Python 3.10 or newer was not found."
        }
        $detectedExecutable = $detectedPython[0]
        $detectedArguments = @($detectedPython | Select-Object -Skip 1)
        & $detectedExecutable @detectedArguments --version
        if ($LASTEXITCODE -ne 0) {
            throw "The detected Python runtime could not be started."
        }
        Write-Host "Python runtime detection succeeded." -ForegroundColor Green
        exit 0
    }

    if ($env:RPA_PYTHON) {
        $pythonPath = (Resolve-Path -LiteralPath $env:RPA_PYTHON).Path
        Write-Host "Using Python from RPA_PYTHON: $pythonPath" -ForegroundColor DarkGray
    }
    else {
        if (-not (Test-Path -LiteralPath $localPython)) {
            $basePython = @(Find-BasePython)
            if ($basePython.Count -eq 0) {
                throw "Python 3.10 or newer was not found. Install Python and enable Add Python to PATH."
            }

            Write-Host "Creating the project Python environment: $venvDir" -ForegroundColor Cyan
            $baseExecutable = $basePython[0]
            $baseArguments = @($basePython | Select-Object -Skip 1)
            & $baseExecutable @baseArguments -m venv $venvDir
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $localPython)) {
                throw "Failed to create the project Python environment: $venvDir"
            }
            Install-ProjectDependencies -PythonPath $localPython
        }

        $pythonPath = (Resolve-Path -LiteralPath $localPython).Path
        if (-not (Test-ProjectDependencies -PythonPath $pythonPath)) {
            Install-ProjectDependencies -PythonPath $pythonPath
        }
    }

    Push-Location -LiteralPath $studioDir
    try {
        & $pythonPath (Join-Path $studioDir "server.py")
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
