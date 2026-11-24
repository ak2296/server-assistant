# Adds a "Server Assistant" app to the Start menu and desktop of this PC.
# It opens the dashboard in its own window (Microsoft Edge app mode), like a normal program.
$ErrorActionPreference = "Stop"
Write-Host ""
Write-Host "  Server Assistant - desktop app" -ForegroundColor Yellow
Write-Host ""

function Test-Server($url) {
    try {
        $r = Invoke-WebRequest -Uri "$url/api/status" -UseBasicParsing -TimeoutSec 3
        return $r.Content -like "*release_check*"
    } catch { return $false }
}

# 1. Find the server: this PC first, then ask.
$server = $null
if (Test-Server "http://localhost:8765") {
    $server = "http://localhost:8765"
    Write-Host "  Found Server Assistant running on this PC."
} else {
    while (-not $server) {
        $addr = Read-Host "  Address of the server (shown in the server window, e.g. 192.168.1.20:8765)"
        $addr = $addr.Trim().TrimEnd("/")
        if (-not $addr) { continue }
        if ($addr -notmatch "^https?://") { $addr = "http://$addr" }
        if ($addr -notmatch ":\d+$") { $addr = "$addr`:8765" }
        Write-Host "  Checking $addr ..."
        if (Test-Server $addr) { $server = $addr }
        else { Write-Host "  Couldn't reach it. Is start.bat running on the server, and is Windows Firewall allowing it?" -ForegroundColor Red }
    }
}

# 2. Find Edge (or Chrome).
$browser = $null
foreach ($key in "msedge.exe", "chrome.exe") {
    foreach ($root in "HKLM:", "HKCU:") {
        $p = (Get-ItemProperty "$root\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$key" -ErrorAction SilentlyContinue)."(default)"
        if ($p -and (Test-Path $p)) { $browser = $p; break }
    }
    if ($browser) { break }
}
if (-not $browser) {
    foreach ($p in "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") {
        if (Test-Path $p) { $browser = $p; break }
    }
}
if (-not $browser) { Write-Host "  Microsoft Edge or Google Chrome is needed." -ForegroundColor Red; exit 1 }

# 3. Copy the icon somewhere permanent and create the shortcuts.
$home_dir = Join-Path $env:LOCALAPPDATA "ServerAssistant"
New-Item -ItemType Directory -Force -Path $home_dir | Out-Null
Copy-Item (Join-Path $PSScriptRoot "ServerAssistant.ico") $home_dir -Force
$icon = Join-Path $home_dir "ServerAssistant.ico"
$profile_dir = Join-Path $home_dir "window"   # own window profile, keeps it separate from your normal browser

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Server Assistant.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Server Assistant.lnk")
)
foreach ($lnk in $targets) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $browser
    $s.Arguments = "--app=$server --user-data-dir=`"$profile_dir`" --window-size=1200,900"
    $s.IconLocation = $icon
    $s.Description = "Server Assistant dashboard"
    $s.Save()
}

Write-Host ""
Write-Host "  Done! 'Server Assistant' is on your desktop and in the Start menu." -ForegroundColor Green
Write-Host "  Dashboard: $server"
Write-Host "  Tip: right-click it in the taskbar while open and choose 'Pin to taskbar'."
Write-Host ""
Start-Process $browser "--app=$server --user-data-dir=`"$profile_dir`" --window-size=1200,900"
