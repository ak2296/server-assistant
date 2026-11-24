@echo off
start "" /b powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0Install-Viewer.ps1"
exit
