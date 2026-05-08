; ATLAS Observatory Server — Inno Setup Script
; Installs atlas_server.exe on the Observatory PC

#define MyAppName "ATLAS Observatory Server"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ZenoSanction"
#define MyAppURL "https://github.com/ZenoSanction/Atlas"
#define MyAppExeName "atlas_server.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\ATLAS\Server
DefaultGroupName=ATLAS Observatory
OutputDir=output
OutputBaseFilename=ATLAS_Server_Setup_v{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\atlas_server.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ATLAS Observatory Server"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall ATLAS Server"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ATLAS Server now"; \
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
    'Enter the IP address of this observatory PC.',
    'This is the IP address that the warm room dashboard will connect to. ' +
    'Set a static IP on this PC in your router''s DHCP reservation table.');
  ObsIPPage.Add('Observatory PC IP Address:', False);
  ObsIPPage.Values[0] := '192.168.1.100';

  ObsNamePage := CreateInputQueryPage(ObsIPPage.ID,
    'Observatory Identity',
    'Name your observatory.',
    'This name will appear in the dashboard and ATLAS reports.');
  ObsNamePage.Add('Observatory Name:', False);
  ObsNamePage.Values[0] := 'My Observatory';

  LatLonPage := CreateInputQueryPage(ObsNamePage.ID,
    'Observatory Location',
    'Enter your observatory''s coordinates.',
    'Used for target planning, rise/set calculations, and weather. ' +
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
