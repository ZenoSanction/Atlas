; ATLAS Dashboard — Inno Setup Script
; Installs atlas_dashboard.exe on the Warm Room PC

#define MyAppName "ATLAS Dashboard"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ZenoSanction"
#define MyAppURL "https://github.com/ZenoSanction/Atlas"
#define MyAppExeName "atlas_dashboard.exe"

[Setup]
AppId={{B2C3D4E5-F6A7-8901-BCDE-F12345678901}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\ATLAS\Dashboard
DefaultGroupName=ATLAS Observatory
OutputDir=output
OutputBaseFilename=ATLAS_Dashboard_Setup_v{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\atlas_dashboard.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ATLAS Dashboard"; Filename: "{app}\{#MyAppExeName}"
Name: "{commondesktop}\ATLAS Dashboard"; Filename: "{app}\{#MyAppExeName}"; \
  Tasks: desktopicon
Name: "{group}\Uninstall ATLAS Dashboard"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ATLAS Dashboard now"; \
  Flags: nowait postinstall skipifsilent

[Code]
var
  ObsIPPage: TInputQueryWizardPage;
  ObsNamePage: TInputQueryWizardPage;
  LatLonPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  ObsIPPage := CreateInputQueryPage(wpWelcome,
    'Observatory Network Setup',
    'Enter the IP address of your observatory PC.',
    'This is the IP address where the ATLAS Server (atlas_server.exe) is running. ' +
    'Make sure the observatory PC has a static IP set in your router.');
  ObsIPPage.Add('Observatory PC IP Address:', False);
  ObsIPPage.Values[0] := '192.168.1.100';

  ObsNamePage := CreateInputQueryPage(ObsIPPage.ID,
    'Observatory Identity',
    'Name your observatory.',
    'This name will appear in the dashboard title bar and ATLAS reports.');
  ObsNamePage.Add('Observatory Name:', False);
  ObsNamePage.Values[0] := 'My Observatory';

  LatLonPage := CreateInputQueryPage(ObsNamePage.ID,
    'Observatory Location',
    'Enter your observatory''s coordinates.',
    'Used for target planning and rise/set calculations. ' +
    'Longitude is negative for West (e.g. -82.06 for central Florida).');
  LatLonPage.Add('Latitude (decimal degrees N):', False);
  LatLonPage.Add('Longitude (decimal degrees, negative=West):', False);
  LatLonPage.Values[0] := '0.0';
  LatLonPage.Values[1] := '0.0';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = ObsIPPage.ID then
  begin
    if Trim(ObsIPPage.Values[0]) = '' then
    begin
      MsgBox('Please enter the observatory PC IP address.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ConfigFile: String;
  ConfigContent: String;
begin
  if CurStep = ssPostInstall then
  begin
    ConfigFile := ExpandConstant('{app}\config.json');
    ConfigContent := '{' + #13#10 +
      '  "observatory_ip": "' + ObsIPPage.Values[0] + '",' + #13#10 +
      '  "observatory_name": "' + ObsNamePage.Values[0] + '",' + #13#10 +
      '  "obs_lat": ' + LatLonPage.Values[0] + ',' + #13#10 +
      '  "obs_lon": ' + LatLonPage.Values[1] + #13#10 +
      '}';
    SaveStringToFile(ConfigFile, ConfigContent, False);
  end;
end;
