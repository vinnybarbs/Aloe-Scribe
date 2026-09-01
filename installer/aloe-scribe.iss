; Inno Setup script for Aloe Scribe (Windows).
; Builds a single AloeScribeSetup.exe that installs the PyInstaller app plus the
; bundled faster-whisper model, with Start menu and desktop shortcuts and an
; uninstaller. Compile with: scripts\build-installer-windows.ps1
;
; Per-user install (no admin prompt). Installs to %LOCALAPPDATA%\Programs so the
; app can write config.toml back. The model lands in {app}\models\<name>, which
; main.py's _resolve_local_model finds next to the .exe.

#define MyAppName "Aloe Scribe"
#define MyAppVersion "1.0"
#define MyAppExeName "Aloe Scribe.exe"
#define MyModelName "faster-distil-whisper-large-v3"

[Setup]
AppId={{8F2A6C4E-1B7D-4E9A-9C3F-2A5B6D8E1F03}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Aloe Scribe
DefaultDirName={localappdata}\Programs\Aloe Scribe
DefaultGroupName=Aloe Scribe
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer\Output
OutputBaseFilename=AloeScribeSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The PyInstaller one-folder build.
; Paths are relative to THIS script's folder (Inno rule), so step up to the
; repo root where PyInstaller and the model fetch actually put things.
Source: "..\dist\Aloe Scribe\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; The transcription model, bundled so the app works offline with no download.
Source: "..\models\{#MyModelName}\*"; DestDir: "{app}\models\{#MyModelName}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Aloe Scribe"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Aloe Scribe"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Aloe Scribe"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Aloe Scribe"; Flags: nowait postinstall skipifsilent
