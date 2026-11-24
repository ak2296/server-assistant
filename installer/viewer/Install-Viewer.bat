@echo off
rem Installs the Server Assistant Viewer for this Windows account (no admin rights needed).
set "DEST=%LOCALAPPDATA%\Programs\Server Assistant Viewer"
mkdir "%DEST%" 2>nul
copy /y "%~dp0viewer.ps1" "%DEST%\" >nul
copy /y "%~dp0favicon.ico" "%DEST%\" >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\Server Assistant.lnk\"); $s.TargetPath='powershell.exe'; $s.Arguments='-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%DEST%\viewer.ps1\"'; $s.IconLocation='%DEST%\favicon.ico'; $s.Save()"
echo.
echo Done. Use the "Server Assistant" icon on your desktop.
pause
