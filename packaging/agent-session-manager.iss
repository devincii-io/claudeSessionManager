#ifndef AppVersion
  #error AppVersion must be supplied, for example: ISCC.exe /DAppVersion=2.0.2 packaging\agent-session-manager.iss
#endif

#define AppName "Agent Session Manager"
#define AppExeName "AgentSessionManager.exe"
#define AppPublisher "Devin Isaac Worbis"
#define AppURL "https://github.com/devincii-io/agent-session-manager"

[Setup]
AppId={{C1AF1AFE-8A71-4F45-984A-4677279C36B7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\Agent Session Manager
DefaultGroupName=Agent Session Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=AgentSessionManager-v{#AppVersion}-Setup
SetupIconFile=..\web\icons\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} installer
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\AgentSessionManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Agent Session Manager"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall Agent Session Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Agent Session Manager"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch Agent Session Manager"; Flags: nowait postinstall skipifsilent
