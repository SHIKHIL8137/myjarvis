; Inno Setup 6 script. Compile with scripts\build-windows.ps1 (or ISCC.exe scripts\setup.iss) after PyInstaller produced dist\jarvis\.
; Installs per-user (no admin). User data lives in %LOCALAPPDATA%\JARVIS and is NOT removed on uninstall.
#define AppVersion "2.0.0"

[Setup]
AppId={{6F1D2C1E-3B0B-4B8A-9F61-5C3A4D1B7A21}
AppName=JARVIS Personal Assistant
AppVersion={#AppVersion}
AppPublisher=JARVIS
DefaultDirName={autopf}\JARVIS
DefaultGroupName=JARVIS
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=JARVIS-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\jarvis.exe
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "autostart"; Description: "Start JARVIS automatically when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\dist\jarvis\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\JARVIS"; Filename: "{app}\jarvis.exe"
Name: "{group}\JARVIS (no tray)"; Filename: "{app}\jarvis.exe"; Parameters: "--no-tray"
Name: "{group}\Installation guide"; Filename: "{app}\INSTALLATION.md"
Name: "{group}\Uninstall JARVIS"; Filename: "{uninstallexe}"
Name: "{userdesktop}\JARVIS"; Filename: "{app}\jarvis.exe"; Tasks: desktopicon

[Registry]
; per-user autostart, only when the box is ticked; removed again on uninstall
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JARVIS"; ValueData: """{app}\jarvis.exe"" --no-browser"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\jarvis.exe"; Description: "Launch JARVIS"; Flags: nowait postinstall skipifsilent
