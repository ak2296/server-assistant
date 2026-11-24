# Server Assistant: repair for the built-in downloader.
# libtorrent (the download engine) exists only for Python 3.9 to 3.13. If this PC's program environment was made with a newer
# Python (3.14), this installs Python 3.13 for your user account only (your own Python stays the default and is not changed),
# rebuilds the program's environment on it and starts the app again. Settings, history and subtitle sources are kept.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Dir = $PSScriptRoot
$log = Join-Path $Dir "fix-downloader.log"
try { Start-Transcript -Path $log -Force | Out-Null } catch {}

function Test-313($exe, $extra) {
    try {
        $out = & $exe @extra -c "import sys; print(sys.version_info[0]*100+sys.version_info[1], sys.maxsize > 2**32)" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out" -match '^313 True')
    } catch { return $false }
}
function Find-313 {
    $c = @(
        @{ Exe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"); Args = @() },
        @{ Exe = (Join-Path $env:ProgramFiles "Python313\python.exe"); Args = @() },
        @{ Exe = "py"; Args = @("-3.13") })
    foreach ($x in $c) {
        if ($x.Exe -like "*\*" -and -not (Test-Path $x.Exe)) { continue }
        if (Test-313 $x.Exe $x.Args) { return $x }
    }
    return $null
}
function Download($url, $file) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try { Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing; return }
    catch { Write-Host "Download with PowerShell failed ($($_.Exception.Message)), trying curl..." }
    & curl.exe -L --fail -o $file $url
    if ($LASTEXITCODE -ne 0) { throw "Could not download $url" }
}
function Is-Running {
    $prefix = (Join-Path $Dir "")
    return [bool](Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) })
}
function Start-App {
    $w = Join-Path $Dir ".venv\Scripts\pythonw.exe"
    if ((Test-Path $w) -and -not (Is-Running)) {
        Start-Process -FilePath $w -ArgumentList "`"$(Join-Path $Dir 'tray.py')`"" -WorkingDirectory $Dir
    }
}
function Stop-Running {
    $prefix = (Join-Path $Dir "")
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
    Start-Sleep -Seconds 2
}

try {
    Write-Host "Looking for Python 3.13..."
    $py = Find-313
    if (-not $py) {
        $file = Join-Path $env:TEMP "python-3.13.1-amd64.exe"
        Write-Host "Downloading Python 3.13 (about 27 MB)..."
        Download "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe" $file
        Write-Host "Installing it for your user account only (your own Python stays the default)..."
        $p = Start-Process -FilePath $file -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=0", "Include_test=0", "Shortcuts=0" -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "The Python installer failed with code $($p.ExitCode)." }
        Remove-Item $file -ErrorAction SilentlyContinue
        $py = Find-313
        if (-not $py) { throw "Python 3.13 was installed but could not be found." }
    }
    Write-Host "Stopping Server Assistant..."
    Stop-Running
    $venv = Join-Path $Dir ".venv"
    $old = Join-Path $Dir (".venv-old-" + (Get-Date -Format "yyyyMMddHHmmss"))
    if (Test-Path $venv) { Rename-Item $venv $old }
    Write-Host "Creating the new program environment..."
    $pyArgs = @($py.Args)
    & $py.Exe @pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
    $vpy = Join-Path $venv "Scripts\python.exe"
    Write-Host "Installing the required components (a minute or two)..."
    & $vpy -m pip install --disable-pip-version-check -q --use-feature=truststore -r (Join-Path $Dir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        & $vpy -m pip install --disable-pip-version-check -q -r (Join-Path $Dir "requirements.txt")
        if ($LASTEXITCODE -ne 0) { throw "Installing the required components failed. Check your internet connection and run this again." }
    }
    foreach ($pkg in (Get-Content (Join-Path $Dir "requirements-optional.txt"))) {
        if ($pkg.Trim()) {
            & $vpy -m pip install --disable-pip-version-check -q --use-feature=truststore $pkg
            if ($LASTEXITCODE -ne 0) { & $vpy -m pip install --disable-pip-version-check -q $pkg }
        }
    }
    & $vpy -c "import libtorrent; print('libtorrent', libtorrent.__version__)"
    if ($LASTEXITCODE -ne 0) { Write-Host "WARNING: libtorrent still could not be loaded." } else { Write-Host "The built-in downloader is ready." }
    Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Starting Server Assistant..."
    Start-App
    Write-Host "Done."
} catch {
    Write-Host ("ERROR: " + $_.Exception.Message)
    # put the old environment back so the app still starts
    $venv = Join-Path $Dir ".venv"
    $olds = Get-ChildItem $Dir -Directory -Filter ".venv-old-*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending
    if ($olds -and -not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
        if (Test-Path $venv) { Remove-Item $venv -Recurse -Force -ErrorAction SilentlyContinue }
        Rename-Item $olds[0].FullName $venv
    }
    try { Start-App } catch { Write-Host ("Could not start the app: " + $_.Exception.Message) }
} finally {
    Stop-Transcript | Out-Null
}
