#define MyAppName "TksToKintone"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "Manekiya"
#define MyAppExeName "TksToKintone.exe"

[Setup]
AppId={{8C19583E-55BA-47BA-93AC-C9F2E1CF3A9F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion=1.2.0.5
SetupIconFile=..\assets\app_icon.ico
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Manekiya\TksToKintone
DefaultGroupName=Manekiya\TksToKintone
DisableProgramGroupPage=yes
OutputDir=.
; インストーラのファイル名はバージョンに関わらず固定する（更新配布の都合上、必ず tks-to-kintone-setup.exe にする）
OutputBaseFilename=tks-to-kintone-setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; GroupDescription: "追加アイコン:"

[Dirs]
Name: "{commonappdata}\Manekiya\TksToKintone"
Name: "{commonappdata}\Manekiya\TksToKintone\logs"
Name: "{commonappdata}\Manekiya\TksToKintone\work"
Name: "{commonappdata}\Manekiya\TksToKintone\error"

[Files]
Source: "..\dist\TksToKintone\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\templates\config.env.sample"; DestDir: "{commonappdata}\Manekiya\TksToKintone"; Flags: ignoreversion onlyifdoesntexist
Source: "..\templates\field_mapping.json.sample"; DestDir: "{commonappdata}\Manekiya\TksToKintone"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{group}\TksToKintone"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\TksToKintone"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "TksToKintoneを起動"; Flags: nowait postinstall skipifsilent
