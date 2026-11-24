# Server Assistant Viewer: opens the dashboard of your server from this PC.
# At home it connects straight to the server. Away from home it starts a small Tailscale helper first.
$ErrorActionPreference = "SilentlyContinue"
$name = "serverassistant"
$data = Join-Path $env:APPDATA "ServerAssistantViewer"
$exe = Join-Path $data "satailnet.exe"
$helperUrl = "https://github.com/ak2296/server-assistant/releases/download/helper-latest/satailnet.exe"
New-Item -ItemType Directory -Force -Path $data | Out-Null

function Find-Browser {
  $c = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
  foreach ($k in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe", "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                   "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")) {
    $v = (Get-ItemProperty $k -ErrorAction SilentlyContinue).'(default)'
    if ($v) { $c = @($v) + $c }
  }
  foreach ($p in $c) { if ($p -and (Test-Path $p)) { return $p } }
  return $null
}
# Opens the dashboard in its own window (no tabs, no address bar), like a normal app.
function Open-App($url) {
  $b = Find-Browser
  if ($b) { return (Start-Process $b -PassThru -ArgumentList "--app=$url", "--user-data-dir=`"$data\window`"", "--window-size=1280,860") }
  Start-Process $url; return $null
}
try { [System.Net.WebRequest]::DefaultWebProxy = $null } catch {}  # a system proxy must never sit between this PC and a server on the home network
function Alive($url, $sec = 2) {
  try { $r = Invoke-RestMethod -Uri "$url/api/ping" -TimeoutSec $sec; return $r.app -eq "server-assistant" } catch { return $false }
}

# 1. At home: the server answers directly.
# The last address that worked goes first; name lookups on a home network can take several seconds, so they get a longer wait.
$last = Join-Path $data "last-server.txt"
$cands = @(); if (Test-Path $last) { $cands += (Get-Content $last -Raw).Trim() }
$cands += @("$name.local", $name)
foreach ($h in ($cands | Where-Object { $_ } | Select-Object -Unique)) {
  if (Alive "http://${h}:8765" 6) {
    # Open by IP address: name lookups on a home network (mDNS) are slow and sometimes fail, which shows up as "Failed to fetch".
    $ip = $null
    try { $ip = ([System.Net.Dns]::GetHostAddresses($h) | Where-Object { $_.AddressFamily -eq "InterNetwork" } | Select-Object -First 1).IPAddressToString } catch {}
    if ($ip -and (Alive "http://${ip}:8765" 4)) { $h = $ip }
    try { Set-Content -Path $last -Value $h } catch {}
    [void](Open-App "http://${h}:8765")
    exit 0
  }
}

# 2. Away from home: go through the private network.
function Show-Msg($t) { Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show($t, "Server Assistant Viewer") }
# Opens the window and, when it is closed, stops the helper so nothing is left running in the background.
function Open-AndWait($url) {
  $w = Open-App $url
  if ($w) {
    $t0 = Get-Date
    try { $w.WaitForExit() } catch {}
    if (((Get-Date) - $t0).TotalSeconds -lt 5) { return }  # the browser handed the window to another process: leave the helper running
  }
  Get-Process -Name satailnet | Where-Object { $_.Path -eq $exe } | Stop-Process -Force
}
# Connected to Tailscale: check that the server really answers before opening the window.
function Wait-Server {
  for ($i = 0; $i -lt 15; $i++) { if (Alive "http://127.0.0.1:8766") { return $true }; Start-Sleep -Seconds 1 }
  return $false
}
if (Alive "http://127.0.0.1:8766") { Open-AndWait "http://127.0.0.1:8766"; exit 0 }
if (-not (Test-Path $exe)) {
  try { Invoke-WebRequest -Uri $helperUrl -OutFile $exe -UseBasicParsing }
  catch { Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show("Could not download the connection helper. Check your internet and try again."); exit 1 }
}
# A helper from an earlier launch may still be holding the port (for example while it waited for the Tailscale sign-in): stop it first.
Get-Process -Name satailnet | Where-Object { $_.Path -eq $exe } | Stop-Process -Force
# ...or by whoever holds port 8766 (a helper started by an older launcher from another folder)
Get-NetTCPConnection -LocalPort 8766 -State Listen | ForEach-Object { $p = Get-Process -Id $_.OwningProcess; if ($p -and $p.ProcessName -eq "satailnet") { $p | Stop-Process -Force } }
Start-Sleep -Milliseconds 800
$log = Join-Path $data "helper.log"
Remove-Item $log -ErrorAction SilentlyContinue
$key = Join-Path $data "authkey.txt"
if (Test-Path $key) { $env:TS_AUTHKEY = (Get-Content $key -Raw).Trim() }
$helperArgs = @("-mode", "connect", "-dir", (Join-Path $data "state"), "-hostname", ("viewer-" + $env:COMPUTERNAME.ToLower()), "-remote", $name, "-listen", "127.0.0.1:8766")
Start-Process $exe -ArgumentList $helperArgs -WindowStyle Hidden -RedirectStandardOutput $log

$opened = $false
for ($i = 0; $i -lt 600; $i++) {
  Start-Sleep -Seconds 1
  foreach ($line in (Get-Content $log -ErrorAction SilentlyContinue)) {
    try { $m = $line | ConvertFrom-Json } catch { continue }
    if ($m.state -eq "login" -and -not $opened) { $opened = $true; Start-Process $m.url }
    if ($m.state -eq "up") {
      if (Wait-Server) { Open-AndWait "http://127.0.0.1:8766"; exit 0 }
      Get-Process -Name satailnet | Where-Object { $_.Path -eq $exe } | Stop-Process -Force
      Show-Msg "Connected to Tailscale, but the server did not answer.`n`nMake sure Server Assistant is running on the server PC (its icon is in the tray by the clock) and that Tailscale is connected there too, then try again."
      exit 1
    }
    if ($m.state -eq "error") { Show-Msg ("Could not connect: " + $m.error); exit 1 }
  }
}
