; agentHUB 安装程序（NSIS 3.x）
; 编译：makensis packaging\installer.nsi
; 产物：dist-installer\agentHUB-<版本>-Setup.exe

Unicode true
SetCompressor /SOLID lzma

!define APP_NAME      "agentHUB"
!define APP_NAME_CN   "agentHUB 会话索引与交接器"
!define APP_VERSION   "0.1.0"
!define APP_PUBLISHER "ZeGL8"
!define APP_EXE       "agentHUB.exe"
!define APP_URL       "https://github.com/ZeGL8/agentHUB"
!define UNINST_KEY    "Software\Microsoft\Windows\CurrentVersion\Uninstall\agentHUB"

Name "${APP_NAME_CN}"
OutFile "..\dist-installer\agentHUB-${APP_VERSION}-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\agentHUB"
InstallDirRegKey HKCU "Software\agentHUB" "InstallDir"
RequestExecutionLevel user          ; 装到当前用户，不需要管理员
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "0.1.0.0"
VIAddVersionKey "ProductName"     "${APP_NAME_CN}"
VIAddVersionKey "ProductVersion"  "${APP_VERSION}"
VIAddVersionKey "FileVersion"     "${APP_VERSION}.0"
VIAddVersionKey "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey "FileDescription" "agentHUB 安装程序"
VIAddVersionKey "LegalCopyright"  "MIT License"

!include "MUI2.nsh"
!include "FileFunc.nsh"

!define MUI_ICON   "agenthub.ico"
!define MUI_UNICON "agenthub.ico"
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "安装 ${APP_NAME_CN}"
!define MUI_WELCOMEPAGE_TEXT "这个工具把 Claude Code、Codex、DSH、Hermes、OpenCode、WorkBuddy 等本地 agent 工具的对话记录汇总成一个可搜索的桌面程序，并支持导出成 Markdown 交接文档。$\r$\n$\r$\n双击图标就是它自己的窗口，不用打开浏览器。它只读取本机文件、只监听 127.0.0.1，不联网也不上传任何内容。$\r$\n$\r$\n点“下一步”继续。"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 agentHUB"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "在桌面创建快捷方式"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION CreateDesktopShortcut
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; ---------- 安装 ----------
Section "主程序" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  File "..\dist-desktop\agentHUB.exe"
  File "agenthub.ico"
  File "..\README.md"

  WriteRegStr HKCU "Software\agentHUB" "InstallDir" "$INSTDIR"

  ; 卸载信息（控制面板可见）
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName"     "${APP_NAME_CN}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion"  "${APP_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher"       "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon"     "$INSTDIR\agenthub.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "URLInfoAbout"    "${APP_URL}"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateDirectory "$SMPROGRAMS\agentHUB"
  CreateShortcut "$SMPROGRAMS\agentHUB\agentHUB.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\agenthub.ico" 0
  CreateShortcut "$SMPROGRAMS\agentHUB\卸载 agentHUB.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

Function CreateDesktopShortcut
  CreateShortcut "$DESKTOP\agentHUB.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\agenthub.ico" 0
FunctionEnd

; ---------- 卸载 ----------
Section "Uninstall"
  ; 先结束可能正在运行的程序，否则 exe 被占用删不掉。
  ; 单文件版运行时会派生子进程，所以要连杀两次并留出退出时间。
  nsExec::Exec 'taskkill /F /IM ${APP_EXE} /T'
  Pop $0
  Sleep 1000
  nsExec::Exec 'taskkill /F /IM ${APP_EXE} /T'
  Pop $0
  Sleep 600

  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\agenthub.ico"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  Delete "$SMPROGRAMS\agentHUB\agentHUB.lnk"
  Delete "$SMPROGRAMS\agentHUB\卸载 agentHUB.lnk"
  RMDir "$SMPROGRAMS\agentHUB"
  Delete "$DESKTOP\agentHUB.lnk"

  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\agentHUB"

  ; 询问是否保留数据（索引与配置在 ~/.agenthub）。静默模式默认保留，不弹框。
  IfSilent KeepData
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "要同时删除索引与配置数据（$PROFILE\.agenthub）吗？$\r$\n$\r$\n选“否”会保留这些数据，方便以后重装继续用。" \
    IDNO KeepData
  RMDir /r "$PROFILE\.agenthub"
  KeepData:
SectionEnd