; ==========================================================================
;  Aegis 安装包脚本（Inno Setup 6）
;
;  前置条件：
;    1. 已用 desktop\build.bat 生成 dist\Aegis\ 目录（本脚本从那里取文件）；
;    2. 已安装 Inno Setup 6（官方免费版即可）。
;
;  编译方式（两种任选）：
;    · 图形界面：右键 installer.iss -> Compile
;    · 命令行：  ISCC.exe desktop\installer.iss
;
;  产物： desktop\Output\Aegis-Setup-{#MyAppVersion}.exe
;
;  覆盖升级（1.4.0 起）：AppId 为固定常量 + DefaultDirName 固定 + [Files] ignoreversion，
;  因此新版安装包会就地覆盖旧版程序文件；%APPDATA%\Aegis 用户数据（配置/会话/密钥/技能）
;  不在安装目录内，升级时不会被触碰。
;
;  ⚠️ 运行环境要求（务必随包告知用户）：
;    · Windows 10 1809（build 17763）或更高版本，仅 64 位；
;    · 需要 Microsoft Edge WebView2 运行时（常青版）。
;      Win11 与已更新的 Win10 通常自带；缺失时 Aegis 启动会弹出中文提示并
;      自动打开下载页：https://developer.microsoft.com/microsoft-edge/webview2/
;      离线环境请提前安装 Evergreen Standalone Installer（离线包）。
;
;  ⚠️ 本文件必须保存为 UTF-8（推荐带 BOM，Inno Setup 6 对带 BOM 的 UTF-8 支持最好）。
; ==========================================================================

#define MyAppName      "Aegis"
#define MyAppPublisher "Aegis"
#define MyAppVersion   "1.8.1"
#define MyAppExeName   "Aegis.exe"

#ifndef MyAppSourceDir
  #define MyAppSourceDir "..\dist\Aegis"
#endif

[Setup]
; ── 应用标识 ──────────────────────────────────────────────
AppId={{9B7E4C21-6D58-4F3A-8E12-5A0C7D3F6B41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

; ── 安装位置与开始菜单 ────────────────────────────────────
; 默认装到 C:\Program Files\Aegis（{autopf} 自动跟随系统位）
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableDirPage=auto
DisableProgramGroupPage=auto

; ── 产物输出 ──────────────────────────────────────────────
OutputDir=Output
OutputBaseFilename=Aegis-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; ── 平台与权限 ────────────────────────────────────────────
; 仅 64 位 Windows；安装到 Program Files 需要管理员权限，
; 允许用户在 UAC 对话框里降权安装到用户目录。
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog

; 需要自定义图标时，把 desktop\aegis.ico 准备好后取消下一行注释
;SetupIconFile=desktop\aegis.ico

; 说明：卸载时**故意不删除**用户数据目录（%APPDATA%\Aegis）。
; 该目录保存会话数据库、用户配置以及系统密钥库中的凭据，
; 随卸载一起删掉等于永久丢失用户数据。需要彻底清理时，
; 请按 desktop\README-打包与分发.md 第 6 节的说明手动删除。

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式:"; Flags: unchecked

[Files]
; 打包产物整体拷进安装目录（含 _internal 子目录）
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Aegis"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 Aegis"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Aegis"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装结束后可选「立即启动 Aegis」
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
