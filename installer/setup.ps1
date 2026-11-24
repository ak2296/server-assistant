# Used by the Server Assistant Setup program. Not meant to be run by hand.
param(
    [Parameter(Mandatory = $true)][ValidateSet("stop", "install")][string]$Action,
    [Parameter(Mandatory = $true)][string]$InstallDir
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Stop-Running {
    # Stop any Server Assistant process that runs from the install folder (tray icon, server, window).
    $prefix = (Join-Path $InstallDir "") 
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
    Start-Sleep -Seconds 1
}

function Test-Python($exe, $extra) {
    try {
        $out = & $exe @extra -c "import sys; print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -match '^\d+$' -and [int]$out -ge 311) { return $true }
    } catch {}
    return $false
}

function Find-Python {
    $local = Join-Path $env:LOCALAPPDATA "Programs\Python"
    $candidates = @()
    foreach ($v in "313", "312", "311", "314") {
        $candidates += @{ Exe = (Join-Path $local "Python$v\python.exe"); Args = @() }
        $candidates += @{ Exe = (Join-Path $env:ProgramFiles "Python$v\python.exe"); Args = @() }
    }
    $candidates += @{ Exe = "py"; Args = @("-3") }
    $candidates += @{ Exe = "python"; Args = @() }
    foreach ($c in $candidates) {
        if ($c.Exe -like "*\*" -and -not (Test-Path $c.Exe)) { continue }
        if (Test-Python $c.Exe $c.Args) { return $c }
    }
    return $null
}

function Install-Python {
    $url = "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe"
    $file = Join-Path $env:TEMP "python-3.13.1-amd64.exe"
    Write-Host "Python 3.11 or newer was not found. Downloading Python 3.13 (about 27 MB)..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
    Write-Host "Installing Python for your user account only..."
    $p = Start-Process -FilePath $file -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=0", "Include_test=0", "Shortcuts=0" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "The Python installer failed with code $($p.ExitCode)." }
    Remove-Item $file -ErrorAction SilentlyContinue
}

if ($Action -eq "stop") { Stop-Running; exit 0 }

try {
    Set-Location $InstallDir
    Stop-Running
    $venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        $py = Find-Python
        if (-not $py) { Install-Python; $py = Find-Python }
        if (-not $py) { throw "Python could not be found or installed." }
        Write-Host "Creating the program environment..."
        $pyArgs = @($py.Args)
        & $py.Exe @pyArgs -m venv (Join-Path $InstallDir ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
    }
    Write-Host "Installing the required components (this can take a minute)..."
    & $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $InstallDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Installing the required components failed. Check your internet connection and run Setup again." }
    Write-Host "Installing the download engine and app-window components..."
    foreach ($pkg in (Get-Content (Join-Path $InstallDir "requirements-optional.txt"))) {
        if ($pkg.Trim()) {
            try { & $venvPy -m pip install --disable-pip-version-check -q $pkg 2>$null } catch {}
            if ($LASTEXITCODE -ne 0) { Write-Host "  (could not install $pkg - that feature will be unavailable)" }
        }
    }
    Write-Host "Done."
    exit 0
} catch {
    Write-Host ("ERROR: " + $_.Exception.Message)
    exit 1
}
