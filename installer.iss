; Inno Setup 스크립트 — build.py가 /D 옵션으로 버전/경로를 전달한다.
; 직접 컴파일: iscc /DMyAppVersion=1.0.0 installer.iss
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SrcExe
  #define SrcExe "build\\dist\\ScrollCapture.exe"
#endif
#ifndef OutDir
  #define OutDir "release"
#endif

#define MyAppName "ScrollCapture"
#define MyAppTitleKo "스크롤 자동 캡처"
#define MyAppPublisher "ScrollCapture"

[Setup]
AppId={{7C1E9B4A-2F3D-4A6B-9C2E-SCROLLCAP0001}
AppName={#MyAppTitleKo}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppTitleKo}
UninstallDisplayName={#MyAppTitleKo} {#MyAppVersion}
OutputDir={#OutDir}
OutputBaseFilename=ScrollCapture_Setup_v{#MyAppVersion}
SetupIconFile=source\icon.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
; 실행 중인 이전 버전을 설치 프로그램이 감지하도록(정중히 닫기 시도)
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 아이콘 만들기"; GroupDescription: "추가 아이콘:"

[Files]
Source: "{#SrcExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppTitleKo}"; Filename: "{app}\ScrollCapture.exe"
Name: "{group}\{cm:UninstallProgram,{#MyAppTitleKo}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppTitleKo}"; Filename: "{app}\ScrollCapture.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ScrollCapture.exe"; Description: "실행"; Flags: nowait postinstall skipifsilent

[Code]
procedure KillRunningApp;
var
  ResultCode: Integer;
begin
  { 실행 중인 이전 버전을 강제 종료(파일 잠금 해제) }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ScrollCapture.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(600);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillRunningApp;   { 파일 복사 직전에 종료 }
  Result := '';
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ScrollCapture.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(400);
  Result := True;
end;
