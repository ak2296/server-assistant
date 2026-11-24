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
  if ($b) { Start-Process $b -ArgumentList "--app=$url", "--user-data-dir=`"$data\window`"", "--window-size=1280,860"; return }
  Start-Process $url
}
function Alive($url) {
  try { $r = Invoke-RestMethod -Uri "$url/api/ping" -TimeoutSec 2; return $r.app -eq "server-assistant" } catch { return $false }
}

# 1. At home: the server answers directly.
foreach ($u in @("http://$name.local:8765", "http://${name}:8765")) {
  if (Alive $u) { Open-App $u; exit 0 }
}

# 2. Away from home: go through the private network.
if (Alive "http://127.0.0.1:8766") { Open-App "http://127.0.0.1:8766"; exit 0 }
if (-not (Test-Path $exe)) {
  try { Invoke-WebRequest -Uri $helperUrl -OutFile $exe -UseBasicParsing }
  catch { Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show("Could not download the connection helper. Check your internet and try again."); exit 1 }
}
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
    if ($m.state -eq "up") { Open-App "http://127.0.0.1:8766"; exit 0 }
    if ($m.state -eq "error") { Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show("Could not connect: " + $m.error); exit 1 }
  }
}
