# Installs the Server Assistant Viewer for this Windows account (no admin rights needed).
Add-Type -AssemblyName PresentationFramework
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $env:LOCALAPPDATA "Programs\Server Assistant Viewer"
$ask = [System.Windows.MessageBox]::Show("Install Server Assistant Viewer on this PC?`n`nIt only opens the dashboard of your server. Nothing is installed on the server side, and Plex is not touched.", "Server Assistant Viewer", "YesNo", "Question")
if ($ask -ne "Yes") { exit 0 }
try {
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  Copy-Item (Join-Path $here "viewer.ps1") $dest -Force
  Copy-Item (Join-Path $here "favicon.ico") $dest -Force
  $args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dest\viewer.ps1`""
  $sh = New-Object -ComObject WScript.Shell
  foreach ($folder in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $l = $sh.CreateShortcut((Join-Path $folder "Server Assistant.lnk"))
    $l.TargetPath = "powershell.exe"; $l.Arguments = $args; $l.IconLocation = "$dest\favicon.ico"; $l.Save()
  }
  [void][System.Windows.MessageBox]::Show("Installed.`n`nA ""Server Assistant"" icon is now on your desktop and in the Start menu. It opens next.", "Server Assistant Viewer", "OK", "Information")
  Start-Process powershell.exe -ArgumentList $args -WindowStyle Hidden
} catch {
  [void][System.Windows.MessageBox]::Show("Install failed: " + $_.Exception.Message, "Server Assistant Viewer", "OK", "Error")
}
