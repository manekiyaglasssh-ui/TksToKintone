#define MyAppName "TksToKintone"
#define MyAppVersion "1.6.1"
#define MyAppPublisher "Manekiya"
#define MyAppExeName "TksToKintone.exe"
#include "..\build\vendor\sumatra\sumatra-config.iss"
#ifndef MyBuildVariant
#define MyBuildVariant "normal"
#endif
#if MyBuildVariant == "no-update"
#define MyOutputBaseFilename "tks-to-kintone-setup-no-update"
#elif MyBuildVariant == "no-helper"
#define MyOutputBaseFilename "tks-to-kintone-setup-no-helper"
#elif MyBuildVariant == "with-helper"
#define MyOutputBaseFilename "tks-to-kintone-setup-with-helper"
#else
#define MyOutputBaseFilename "tks-to-kintone-setup"
#endif

[Setup]
AppId={{8C19583E-55BA-47BA-93AC-C9F2E1CF3A9F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion=1.6.1.45
SetupIconFile=..\assets\app_icon.ico
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Manekiya\TksToKintone
DefaultGroupName=Manekiya\TksToKintone
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename={#MyOutputBaseFilename}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; FilesInUse 対策。更新時は本体からこのインストーラを直接起動し、
; その後すぐ本体を終了する。念のため使用中アプリの自動クローズを有効化し、
; 再起動要求は抑制する。
CloseApplications=yes
RestartApplications=no

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
#if MyBuildVariant == "with-helper"
Source: "..\dist\TksToKintone\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#else
Source: "..\dist\TksToKintone\*"; DestDir: "{app}"; Excludes: "tks_update_helper.exe"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif
Source: "..\templates\config.env.sample"; DestDir: "{commonappdata}\Manekiya\TksToKintone"; Flags: ignoreversion onlyifdoesntexist
Source: "..\templates\field_mapping.json.sample"; DestDir: "{commonappdata}\Manekiya\TksToKintone"; Flags: ignoreversion onlyifdoesntexist
; OLAPリクエストテンプレートは PyInstaller 同梱（dist 一式コピー）に加えて、
; リポジトリ docs/olap から直接 {app}\_internal\docs\olap へも明示配置する。
; 万一 PyInstaller のバンドルから漏れても、同梱側からの自己復旧が必ず成立するよう
; 二重に保証する（旧テンプレート削除セクションの実行後、ここで最新を再配置）。
Source: "..\docs\olap\kakou_request_template.json"; DestDir: "{app}\_internal\docs\olap"; Flags: ignoreversion
Source: "..\docs\olap\soba_request_template.json"; DestDir: "{app}\_internal\docs\olap"; Flags: ignoreversion
; 固定・検証済みの公式installerをセットアップ内部へ同梱し、必要な場合だけ{tmp}へ展開する。
; TksToKintoneのインストール先には残さない。
Source: "..\build\vendor\sumatra\{#SumatraInstallerFilename}"; Flags: dontcopy deleteafterinstall
Source: "..\third_party_licenses\SumatraPDF.txt"; DestDir: "{app}\third_party_licenses"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\tks_update_helper.exe"
; 旧版で同梱していた背景除去エンジン・モデルをアプリ配下から除去する。
; ユーザー共通キャッシュや他アプリのデータは対象にしない。
Type: filesandordirs; Name: "{app}\_internal\assets\rembg"
Type: filesandordirs; Name: "{app}\_internal\rembg"
Type: filesandordirs; Name: "{app}\_internal\onnxruntime"
Type: filesandordirs; Name: "{app}\_internal\pymatting"
Type: filesandordirs; Name: "{app}\_internal\rembg-*.dist-info"
Type: filesandordirs; Name: "{app}\_internal\onnxruntime-*.dist-info"
Type: filesandordirs; Name: "{app}\_internal\pymatting-*.dist-info"
; 更新時に古いOLAPリクエストテンプレートを必ず削除してから再配置する。
; 古いテンプレートには「OP区分」が含まれず、CSV出力で㎡/総㎡が空欄になるため。
; PyInstaller onedir の同梱データは {app}\_internal\docs\olap 配下に展開される。
Type: files; Name: "{app}\_internal\docs\olap\*.json"
Type: files; Name: "{app}\_internal\docs\olap\kakou_request_template.json"
Type: files; Name: "{app}\_internal\docs\olap\soba_request_template.json"
; 1.5.12以前にTksToKintone自身が管理していたポータブル版だけを削除する。
; 独立インストール先・レジストリ・ユーザー設定は一切削除しない。
Type: filesandordirs; Name: "{app}\tools\SumatraPDF"
Type: filesandordirs; Name: "{app}\_internal\tools\SumatraPDF"

[Icons]
Name: "{group}\TksToKintone"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\TksToKintone"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "TksToKintoneを起動"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Parameters: "--post-update"; WorkingDir: "{app}"; Flags: nowait runasoriginaluser; Check: ShouldRelaunchAfterUpdate; BeforeInstall: LogUpdateRelaunchStart; AfterInstall: LogUpdateRelaunchSubmitted

[Code]
const
  SumatraUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\SumatraPDF';

function BooleanToLog(Value: Boolean): String;
begin
  if Value then
    Result := 'true'
  else
    Result := 'false';
end;

function ShouldRelaunchAfterUpdate(): Boolean;
var
  ExecutablePath: String;
  RelaunchValue: String;
  ExeExists: Boolean;
begin
  { /RELAUNCHAPP=1 is supplied only by the verified in-app update flow. }
  RelaunchValue := Trim(ExpandConstant('{param:RELAUNCHAPP|0}'));
  ExecutablePath := ExpandConstant('{app}\{#MyAppExeName}');
  ExeExists := FileExists(ExecutablePath);
  Result := (CompareText(RelaunchValue, '1') = 0) and ExeExists;

  Log('event=update_relaunch_parameter');
  Log(Format('value=%s', [RelaunchValue]));
  Log('event=update_relaunch_check');
  Log(Format('exe_exists=%s', [BooleanToLog(ExeExists)]));
  Log(Format('result=%s', [BooleanToLog(Result)]));
  Log(Format('event=update_relaunch_check value=%s exe_exists=%s result=%s', [RelaunchValue,
    BooleanToLog(ExeExists), BooleanToLog(Result)]));

  if not Result then
  begin
    if CompareText(RelaunchValue, '1') <> 0 then
      Log('event=update_relaunch_skipped reason=parameter_not_enabled')
    else
      Log('event=update_relaunch_failed reason=executable_missing');
  end;
end;

procedure LogUpdateRelaunchStart();
begin
  Log('event=update_relaunch_start');
  Log(Format('exe=%s', [ExpandConstant('{app}\{#MyAppExeName}')]));
  Log('parameters=--post-update');
end;

procedure LogUpdateRelaunchSubmitted();
begin
  { Inno Setup's own [Run] log records the OS error if process creation fails. }
  Log('event=update_relaunch_submitted original_user=true');
end;

function ValidSumatraExe(const Candidate: String): Boolean;
begin
  Result :=
    (CompareText(ExtractFileName(Candidate), 'SumatraPDF.exe') = 0) and
    FileExists(Candidate);
end;

function DisplayIconExePath(const Value: String): String;
var
  Text: String;
  ClosingQuote: Integer;
  ExeAt: Integer;
begin
  Result := '';
  Text := Trim(Value);
  if Text = '' then
    Exit;
  if Text[1] = '"' then
  begin
    Delete(Text, 1, 1);
    ClosingQuote := Pos('"', Text);
    if ClosingQuote > 0 then
      Result := Copy(Text, 1, ClosingQuote - 1);
  end
  else
  begin
    ExeAt := Pos('.exe', Lowercase(Text));
    if ExeAt > 0 then
      Result := Copy(Text, 1, ExeAt + 3);
  end;
  Result := Trim(Result);
end;

function CheckSumatraRegistry(
  const RootKey: Integer;
  const RootName: String;
  var FoundPath: String
): Boolean;
var
  InstallLocation: String;
  DisplayIcon: String;
  Candidate: String;
begin
  Result := False;
  InstallLocation := '';
  DisplayIcon := '';
  RegQueryStringValue(RootKey, SumatraUninstallKey, 'InstallLocation', InstallLocation);
  RegQueryStringValue(RootKey, SumatraUninstallKey, 'DisplayIcon', DisplayIcon);
  Log(Format(
    'event=sumatra_dependency_probe source=%s key=%s install_location=%s display_icon=%s', [
    RootName, SumatraUninstallKey, InstallLocation, DisplayIcon]));
  if InstallLocation <> '' then
  begin
    Candidate := AddBackslash(RemoveQuotes(Trim(InstallLocation))) + 'SumatraPDF.exe';
    if ValidSumatraExe(Candidate) then
    begin
      FoundPath := Candidate;
      Result := True;
      Exit;
    end;
  end;
  Candidate := DisplayIconExePath(DisplayIcon);
  if ValidSumatraExe(Candidate) then
  begin
    FoundPath := Candidate;
    Result := True;
  end;
end;

function CheckSumatraStandardPath(const Candidate: String; var FoundPath: String): Boolean;
begin
  Log(Format('event=sumatra_dependency_probe source=standard_path path=%s exists=%d', [
    Candidate, Ord(FileExists(Candidate))]));
  Result := ValidSumatraExe(Candidate);
  if Result then
    FoundPath := Candidate;
end;

function FindInstalledSumatraPdf(var FoundPath: String): Boolean;
begin
  FoundPath := '';
  { HKCU must be checked before HKLM; each hive checks both registry views. }
  Result :=
    CheckSumatraRegistry(HKCU64, 'HKCU64', FoundPath) or
    CheckSumatraRegistry(HKCU32, 'HKCU32', FoundPath) or
    CheckSumatraRegistry(HKLM64, 'HKLM64', FoundPath) or
    CheckSumatraRegistry(HKLM32, 'HKLM32', FoundPath) or
    CheckSumatraStandardPath(
      ExpandConstant('{localappdata}\SumatraPDF\SumatraPDF.exe'), FoundPath) or
    CheckSumatraStandardPath(
      ExpandConstant('{autopf}\SumatraPDF\SumatraPDF.exe'), FoundPath) or
    CheckSumatraStandardPath(
      ExpandConstant('{pf}\SumatraPDF\SumatraPDF.exe'), FoundPath) or
    CheckSumatraStandardPath(
      ExpandConstant('{pf32}\SumatraPDF\SumatraPDF.exe'), FoundPath);
end;

function IsSumatraPdfInstalled(): Boolean;
var
  FoundPath: String;
begin
  Result := FindInstalledSumatraPdf(FoundPath);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  SumatraPath: String;
  InstallerPath: String;
  ResultCode: Integer;
  Started: Boolean;
begin
  Result := '';
  if FindInstalledSumatraPdf(SumatraPath) then
  begin
    Log(Format(
      'event=sumatra_dependency_check status=already_installed path=%s action=skip', [
      SumatraPath]));
    Exit;
  end;

  Log('event=sumatra_dependency_check status=missing action=install');
  try
    ExtractTemporaryFile('{#SumatraInstallerFilename}');
  except
    Result :=
      'SumatraPDFインストーラーを展開できませんでした。' + #13#10 +
      '印刷機能を使用するためにSumatraPDFが必要です。' + #13#10#13#10 +
      'セットアップを再実行するか、管理者へお問い合わせください。';
    Log('event=sumatra_dependency_install status=extract_failed');
    Exit;
  end;

  InstallerPath := ExpandConstant('{tmp}\{#SumatraInstallerFilename}');
  Started := Exec(
    InstallerPath,
    '-install -silent -all-users',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode);
  Log(Format(
    'event=sumatra_dependency_install started=%d exit_code=%d arguments="-install -silent -all-users"', [
    Ord(Started), ResultCode]));
  DeleteFile(InstallerPath);

  if not Started then
  begin
    Result :=
      'SumatraPDFインストーラーを起動できませんでした。' + #13#10 +
      '印刷機能を使用するためにSumatraPDFが必要です。' + #13#10#13#10 +
      'セットアップを再実行するか、管理者へお問い合わせください。';
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    Result :=
      Format('SumatraPDFをインストールできませんでした（終了コード: %d）。', [ResultCode]) + #13#10 +
      '印刷機能を使用するためにSumatraPDFが必要です。' + #13#10#13#10 +
      'セットアップを再実行するか、管理者へお問い合わせください。';
    Exit;
  end;
  if not FindInstalledSumatraPdf(SumatraPath) then
  begin
    Log('event=sumatra_dependency_install status=exe_not_found_after_success');
    Result :=
      'SumatraPDFをインストールできませんでした。' + #13#10 +
      'インストーラーは終了しましたが、SumatraPDF.exeが見つかりません。' + #13#10 +
      '印刷機能を使用するためにSumatraPDFが必要です。' + #13#10#13#10 +
      'セットアップを再実行するか、管理者へお問い合わせください。';
    Exit;
  end;
  Log(Format(
    'event=sumatra_dependency_install status=installed path=%s action=continue', [
    SumatraPath]));
end;
