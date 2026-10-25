; ============================================================================
;  DreamRole 安装程序（NSIS 3 / Unicode）
;
;  编译（务必带 /INPUTCHARSET UTF8，否则中文会变成乱码）：
;      makensis /INPUTCHARSET UTF8 installer\dreamrole.nsi
;  平时不用手敲，走 scripts\build_installer.py 就行 —— 它会先打包 exe 再编译本脚本。
;
;  几个刻意做成这样的决定，都踩过坑：
;
;  1. **按用户安装，不要管理员权限**（RequestExecutionLevel user）。
;     装到 %LOCALAPPDATA%\Programs\DreamRole，跟 VS Code 的用户版一个位置。
;     好处是双击就能装完，不弹 UAC；代价是「所有用户」看不到它 ——
;     一个自己用的 agent 工具，这个取舍是对的。
;
;  2. **程序目录和数据目录分开**。程序在 %LOCALAPPDATA%\Programs\DreamRole，
;     数据在 %LOCALAPPDATA%\DreamRole。这样卸载才能心安理得清空程序目录，
;     升级重装也不会碰到几十条会话和记忆。程序靠 install.marker 判断自己是安装版
;     （没有这个标记就当绿色版，数据放 exe 同级的 data\ 里）。
;
;  3. **装完把 exe 的完整性级别改回 Medium**（见 Section 里那句 icacls）。
;     如果安装目录本身带着 Low 强制标签（有些沙箱/安全软件会这么干），
;     新写进去的 exe 会继承 Low，而 onefile 的 exe 启动要先往 %TEMP% 解包 ——
;     Low 进程解不了，于是「双击完全没反应」，没有报错也没有日志。
;     这句是兜底，正常情况下什么也不改变。
; ============================================================================

Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"

; 版本号与产物路径由外部传入（build_installer.py 从 app/config.py 里读），
; 手敲编译时用下面这些默认值。
!ifndef APP_VERSION
  !define APP_VERSION "1.6.9"
!endif
!ifndef SRC_EXE
  !define SRC_EXE "..\dist\DreamRole.exe"
!endif

!define APP_NAME      "DreamRole"
!define APP_EXE       "DreamRole.exe"
!define APP_ID        "DreamRole"
!define APP_PUBLISHER "DreamRole"
!define APP_URL       "https://localhost"
!define DATA_DIR      "$LOCALAPPDATA\${APP_NAME}"
!define UNINST_KEY    "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "..\${APP_NAME}-Setup-${APP_VERSION}.exe"

; 已经装过就沿用上次的目录（用户可能改过），没装过就用默认位置
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "Software\${APP_ID}" "InstallDir"

RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64
SetDatablockOptimize on
CRCCheck on
BrandingText "${APP_NAME} ${APP_VERSION}"

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName"     "${APP_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APP_NAME} 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion"     "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion"  "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "OriginalFilename" "${APP_NAME}-Setup-${APP_VERSION}.exe"
VIAddVersionKey /LANG=2052 "LegalCopyright"  "© 2026 ${APP_PUBLISHER}"

; ---------------------------------------------------------------- 界面 ------
!define MUI_ICON   "..\assets\dreamrole.ico"
!define MUI_UNICON "..\assets\dreamrole.ico"
!define MUI_ABORTWARNING
!define MUI_ABORTWARNING_TEXT "确定要放弃安装吗？"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP_NOSTRETCH ""

!define MUI_WELCOMEPAGE_TITLE "安装 ${APP_NAME} ${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "DreamRole 是一个装在自己电脑上的 AI agent 工作台：多模型、工作区、工具审批、改动与退回都在里面。$\r$\n$\r$\n安装位置：$LOCALAPPDATA\Programs\${APP_NAME}$\r$\n你的数据：$LOCALAPPDATA\${APP_NAME}（升级和卸载都不会动它）$\r$\n$\r$\n只装给当前用户，不需要管理员权限，也不会改动系统设置。$\r$\n$\r$\n点「下一步」开始，点「安装」就完成。"

!define MUI_FINISHPAGE_TITLE "${APP_NAME} 装好了"
!define MUI_FINISHPAGE_TEXT "桌面上已经有 ${APP_NAME} 的快捷方式，开始菜单里也有。$\r$\n$\r$\n配置、会话、记忆、产物都存在：$\r$\n$LOCALAPPDATA\${APP_NAME}$\r$\n$\r$\n第一次启动会让你填模型服务的 API Key。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "现在启动 ${APP_NAME}"
!define MUI_FINISHPAGE_NOREBOOTSUPPORT

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------- 装 ------
Function .onInit
  ; 覆盖安装：告知一下，数据不动。静默安装（/S）直接过，不弹窗。
  ReadRegStr $0 HKCU "Software\${APP_ID}" "Version"
  ${If} $0 != ""
  ${AndIfNot} ${Silent}
    MessageBox MB_OKCANCEL|MB_ICONINFORMATION \
      "检测到已安装 ${APP_NAME} $0。$\r$\n$\r$\n继续会把程序文件升级到 ${APP_VERSION}，你的配置、会话和记忆不受影响。" \
      /SD IDOK IDOK upgrade_warn
    Abort
  ${EndIf}
  upgrade_warn:
FunctionEnd

Section "DreamRole 主程序" SEC_MAIN
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  SetOverwrite on

  ; 先把正在跑的实例关掉：exe 被占用时覆盖安装会失败，
  ; 而 NSIS 的报错是「无法写入文件」，用户完全不知道是软件还开着。
  ; 先礼后兵：不带 /F 让它自己关窗口，700ms 后还在就强杀。
  ${If} ${FileExists} "$INSTDIR\${APP_EXE}"
    nsExec::Exec 'taskkill /IM "${APP_EXE}"'
    Pop $0
    Sleep 700
    nsExec::Exec 'taskkill /F /IM "${APP_EXE}"'
    Pop $0
    Sleep 300
  ${EndIf}

  File "/oname=${APP_EXE}" "${SRC_EXE}"

  ; 「我是安装版」的标记。程序看到它就把数据放到 %LOCALAPPDATA%\DreamRole，
  ; 而不是 exe 旁边的 data\（那是绿色版的行为）。
  FileOpen $0 "$INSTDIR\install.marker" w
  FileWrite $0 "${APP_VERSION}"
  FileClose $0

  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; 桌面 + 开始菜单。图标直接用 exe 自己的，不用另拷 .ico。
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" \
                 "$INSTDIR\${APP_EXE}" 0 SW_SHOWNORMAL "" "DreamRole · 本地 AI agent 工作台"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" \
                 "$INSTDIR\${APP_EXE}" 0 SW_SHOWNORMAL "" "DreamRole · 本地 AI agent 工作台"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\uninstall.exe" "" \
                 "$INSTDIR\uninstall.exe" 0 SW_SHOWNORMAL "" "卸载 DreamRole（会问你留不留数据）"

  ; 「应用和功能」里的一条记录
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayName"     "${APP_NAME}"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayVersion"  "${APP_VERSION}"
  WriteRegStr   HKCU "${UNINST_KEY}" "Publisher"       "${APP_PUBLISHER}"
  WriteRegStr   HKCU "${UNINST_KEY}" "URLInfoAbout"    "${APP_URL}"
  WriteRegStr   HKCU "${UNINST_KEY}" "DisplayIcon"     "$INSTDIR\${APP_EXE},0"
  WriteRegStr   HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr   HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

  ; 记下装在哪、什么版本（下次覆盖安装沿用目录，卸载时也知道版本）
  WriteRegStr HKCU "Software\${APP_ID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APP_ID}" "Version"    "${APP_VERSION}"

  ; 让 Win+R 里直接敲 DreamRole 也能起来
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\App Paths\${APP_EXE}" "" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\App Paths\${APP_EXE}" "Path" "$INSTDIR"

  ; 兜底：万一安装目录带着 Low 强制标签，把这层继承下来的标记摘掉。
  ; 不摘的话 exe 启动时解包被拒，双击等于没反应（无日志、无弹窗、无事件）。
  ; 正常情况下这句什么也不改变，失败也不影响安装。
  nsExec::Exec 'icacls "$INSTDIR\${APP_EXE}" /setintegritylevel Medium'
  Pop $0
SectionEnd

; ---------------------------------------------------------------- 卸 ------
Section "Uninstall"
  SetShellVarContext current

  nsExec::Exec 'taskkill /IM "${APP_EXE}"'
  Pop $0
  Sleep 700
  nsExec::Exec 'taskkill /F /IM "${APP_EXE}"'
  Pop $0
  Sleep 300

  ; 快捷方式：桌面、开始菜单，以及「某个快捷方式被拖到别处」的情况不管
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${APP_NAME}"

  ; 程序文件。程序目录里的东西是我们装的，可以放心删；
  ; 用户要是手动往里塞了别的东西，RMDir 删不掉会自己留住，不会误删。
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\install.marker"
  Delete "$INSTDIR\uninstall.exe"
  RMDir  "$INSTDIR"
  RMDir  "$LOCALAPPDATA\Programs\${APP_NAME}"

  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\App Paths\${APP_EXE}"
  DeleteRegKey HKCU "Software\${APP_ID}"

  ; 数据单独问。默认「否」—— 卸载手滑一次不该把几十条会话和记忆一起干掉。
  ; 静默卸载（/S）不去猜，按「保留」处理，删不删交给调用方自己决定。
  ${IfNot} ${Silent}
    ${If} ${FileExists} "${DATA_DIR}\settings.json"
      MessageBox MB_YESNO|MB_ICONQUESTION \
        "程序已经卸载。$\r$\n$\r$\n要不要连你的数据一起删掉？$\r$\n$\r$\n${DATA_DIR}$\r$\n（配置、会话记录、记忆、产物都在这里）$\r$\n$\r$\n选「是」= 彻底删除；选「否」= 保留，以后再装回来还能接着用。" \
        /SD IDNO IDNO keep_data
      RMDir /r "${DATA_DIR}"
    ${EndIf}
  ${EndIf}
  keep_data:
SectionEnd
