; 特退金脱退 処理漏れ防止 のインストーラー（Inno Setup 6）
; ビルド:  iscc /DAppVersion=1.1.0 installer\TokutaiApp.iss   （事前に PyInstaller で dist\TokutaiApp を作っておく）
; 出力:    installer\Output\TokutaiApp-Setup-<版>.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; AppId は、更新のときに「同じアプリ」と見分けるための固定値。変えないこと。
AppId={{95119B7D-4BE8-4754-8371-BE1CAE3A2B48}
AppName=特退金脱退 処理漏れ防止
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
; 管理者権限なしで、利用者のフォルダに入れる（自動更新も、管理者の確認なしでできる）
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\TokutaiApp
DefaultGroupName=特退金脱退 処理漏れ防止
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\TokutaiApp.exe
SetupIconFile=..\tokutai\data\app.ico
OutputDir=Output
OutputBaseFilename=TokutaiApp-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; 起動中のアプリは、自動で閉じて入れ替える（更新のとき）
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作る"; GroupDescription: "ショートカット:"

[Files]
Source: "..\dist\TokutaiApp\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\特退金脱退 処理漏れ防止"; Filename: "{app}\TokutaiApp.exe"
Name: "{autodesktop}\特退金脱退 処理漏れ防止"; Filename: "{app}\TokutaiApp.exe"; Tasks: desktopicon

[Run]
; インストール（更新）が終わったら、アプリを起動する。更新の静かなインストールでも、起動し直す。
Filename: "{app}\TokutaiApp.exe"; Description: "アプリを起動する"; Flags: nowait postinstall
