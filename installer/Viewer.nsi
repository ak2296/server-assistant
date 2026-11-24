; Server Assistant Viewer: a tiny installer for PCs that only OPEN the dashboard (no Python, no server).
Unicode true
!include "MUI2.nsh"
!ifndef VERSION
  !define VERSION "1.0"
!endif
!define APP "Server Assistant Viewer"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ServerAssistantViewer"
Name "${APP}"
OutFile "ServerAssistant-Viewer-Setup.exe"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\Server Assistant Viewer"
SetCompressor /SOLID lzma
!define MUI_ICON "viewer\favicon.ico"
!define MUI_UNICON "viewer\favicon.ico"
!define MUI_WELCOMEPAGE_TITLE "Welcome to ${APP}"
!define MUI_WELCOMEPAGE_TEXT "This PC will only open the Server Assistant dashboard of your server. It does not install the server itself, Python or anything that touches Plex.$\r$\n$\r$\nAt home it connects directly. Away from home it uses Tailscale automatically."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open the dashboard now"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function LaunchApp
  Exec 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "$INSTDIR\viewer.ps1"'
FunctionEnd

Section "Install"
  SetOutPath "$INSTDIR"
  File "viewer\viewer.ps1"
  File "viewer\favicon.ico"
  CreateShortCut "$SMPROGRAMS\Server Assistant.lnk" "powershell.exe" '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "$INSTDIR\viewer.ps1"' "$INSTDIR\favicon.ico" 0
  CreateShortCut "$DESKTOP\Server Assistant.lnk" "powershell.exe" '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "$INSTDIR\viewer.ps1"' "$INSTDIR\favicon.ico" 0
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
SectionEnd

Section "Uninstall"
  nsExec::ExecToLog 'taskkill /f /im satailnet.exe'
  Delete "$SMPROGRAMS\Server Assistant.lnk"
  Delete "$DESKTOP\Server Assistant.lnk"
  Delete "$INSTDIR\viewer.ps1"
  Delete "$INSTDIR\favicon.ico"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  RMDir /r "$APPDATA\ServerAssistantViewer"
  DeleteRegKey HKCU "${UNINST_KEY}"
SectionEnd
