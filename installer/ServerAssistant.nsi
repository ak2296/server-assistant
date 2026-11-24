; Server Assistant Setup (builds with NSIS: makensis installer/ServerAssistant.nsi)
Unicode true
!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!define APP "Server Assistant"
!ifndef VERSION
  !define VERSION "1.2.0"
!endif
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ServerAssistant"
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"

Name "${APP}"
OutFile "ServerAssistant-Setup.exe"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\Server Assistant"
InstallDirRegKey HKCU "Software\ServerAssistant" "InstallDir"
SetCompressor /SOLID lzma
BrandingText "${APP} ${VERSION}"

!define MUI_ICON "stage\app\static\favicon.ico"
!define MUI_UNICON "stage\app\static\favicon.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Welcome to ${APP}"
!define MUI_WELCOMEPAGE_TEXT "This installs ${APP} for your Windows account. If it is already installed, Setup updates it and keeps all your settings and history.$\r$\n$\r$\nNo administrator rights are needed. If Python 3.11 or newer is missing, Setup downloads and installs it for you (internet needed)."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open ${APP} now"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!define MUI_FINISHPAGE_SHOWREADME
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Start ${APP} when Windows starts"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION SetAutostart

Var ImportDir
Var ImportText
Var Dialog

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
Page custom ImportPage ImportLeave
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function ImportPage
  ; Only offer the import on a first install (no settings yet)
  ${If} ${FileExists} "$APPDATA\ServerAssistant\config.toml"
    Abort
  ${EndIf}
  nsDialogs::Create 1018
  Pop $Dialog
  ${NSD_CreateLabel} 0 0 100% 36u "Do you already use ${APP} from an unzipped folder? To keep your settings and history, choose that folder (the one with config.toml in it). Otherwise just click Next."
  Pop $0
  ${NSD_CreateText} 0 46u 78% 13u ""
  Pop $ImportText
  ${NSD_CreateBrowseButton} 80% 45u 20% 14u "Browse..."
  Pop $0
  ${NSD_OnClick} $0 ImportBrowse
  nsDialogs::Show
FunctionEnd

Function ImportBrowse
  nsDialogs::SelectFolderDialog "Choose your old Server Assistant folder" ""
  Pop $0
  ${If} $0 != error
    ${NSD_SetText} $ImportText $0
  ${EndIf}
FunctionEnd

Function ImportLeave
  ${NSD_GetText} $ImportText $ImportDir
FunctionEnd

Function LaunchApp
  SetOutPath "$INSTDIR"
  Exec '"$INSTDIR\.venv\Scripts\pythonw.exe" "$INSTDIR\tray.py" --open'
FunctionEnd

Function SetAutostart
  WriteRegStr HKCU "${RUN_KEY}" "ServerAssistant" '"$INSTDIR\.venv\Scripts\pythonw.exe" "$INSTDIR\tray.py"'
FunctionEnd

Section "Install"
  InitPluginsDir
  File "/oname=$PLUGINSDIR\setup.ps1" "setup.ps1"
  CreateDirectory "$INSTDIR"
  DetailPrint "Stopping ${APP} if it is running..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\setup.ps1" -Action stop -InstallDir "$INSTDIR"'
  Pop $0

  SetOutPath "$INSTDIR"
  File /r "stage\*.*"
  CreateDirectory "$INSTDIR\installer"
  File "/oname=$INSTDIR\installer\setup.ps1" "setup.ps1"
  FileOpen $0 "$INSTDIR\installed.flag" w
  FileWrite $0 "1"
  FileClose $0

  ; Settings and history live in %APPDATA%\ServerAssistant so updates never touch them
  CreateDirectory "$APPDATA\ServerAssistant"
  ${If} $ImportDir != ""
    ${If} ${FileExists} "$ImportDir\config.toml"
      DetailPrint "Copying your settings from $ImportDir"
      CopyFiles /SILENT "$ImportDir\config.toml" "$APPDATA\ServerAssistant\config.toml"
      CopyFiles /SILENT "$ImportDir\server_assistant.db" "$APPDATA\ServerAssistant\server_assistant.db"
      CopyFiles /SILENT "$ImportDir\subtitle_sources.json" "$APPDATA\ServerAssistant\subtitle_sources.json"
    ${Else}
      DetailPrint "No config.toml found in $ImportDir, nothing copied."
    ${EndIf}
  ${EndIf}

  DetailPrint "Setting up Python and the required components..."
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\setup.ps1" -Action install -InstallDir "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "Setup could not finish installing the required components. Read the messages in the details list above, fix the problem (often: no internet) and run Setup again."
    Abort
  ${EndIf}

  SetOutPath "$INSTDIR"
  CreateShortCut "$SMPROGRAMS\${APP}.lnk" "$INSTDIR\.venv\Scripts\pythonw.exe" '"$INSTDIR\tray.py" --open' "$INSTDIR\app\static\favicon.ico" 0
  CreateShortCut "$DESKTOP\${APP}.lnk" "$INSTDIR\.venv\Scripts\pythonw.exe" '"$INSTDIR\tray.py" --open' "$INSTDIR\app\static\favicon.ico" 0

  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "Software\ServerAssistant" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Server Assistant"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\app\static\favicon.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\installer\setup.ps1" -Action stop -InstallDir "$INSTDIR"'
  Pop $0
  Delete "$SMPROGRAMS\${APP}.lnk"
  Delete "$DESKTOP\${APP}.lnk"
  DeleteRegValue HKCU "${RUN_KEY}" "ServerAssistant"
  RMDir /r "$INSTDIR\.venv"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\installer"
  Delete "$INSTDIR\run.py"
  Delete "$INSTDIR\tray.py"
  Delete "$INSTDIR\appwindow.py"
  Delete "$INSTDIR\requirements.txt"
  Delete "$INSTDIR\requirements-optional.txt"
  Delete "$INSTDIR\config.example.toml"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\VERSION"
  Delete "$INSTDIR\installed.flag"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\ServerAssistant"
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete your settings, history and subtitle sources?$\r$\n($APPDATA\ServerAssistant)$\r$\n$\r$\nChoose No to keep them for a future install." IDNO +2
  RMDir /r "$APPDATA\ServerAssistant"
SectionEnd
