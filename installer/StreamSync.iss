; StreamSync's Windows installer (Inno Setup 6).
;
; Per user, so it never asks for admin: StreamSync goes into
; %LOCALAPPDATA%\Programs\StreamSync, which is also the one place the
; in-app updater will replace (updater.installed_exe_path). That is why
; the folder cannot be changed here - an install anywhere else could not
; update itself.
;
; Built by .github/workflows/build-windows.yml from PyInstaller's
; dist\StreamSync folder:
;   iscc /DAppVersion=2.0.0 /DOutputBase=StreamSync-2.0.0-windows-x64-abc1234-setup installer\StreamSync.iss
;
; Updates run this same installer silently (updater.SETUP_ARGS):
; /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /relaunch=1. It closes a
; running StreamSync first and, given /relaunch=1, starts the new one.
; Settings and logs live in the user's home folder, outside {app}, so an
; update or an uninstall leaves them alone.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\StreamSync"
#endif
#ifndef OutputBase
  #define OutputBase "StreamSync-setup"
#endif

[Setup]
; Never change the AppId: Windows knows an installed StreamSync by it, and
; a new one would install a second copy beside the first.
AppId={{DD7E9DD2-921F-4429-9816-2D38D54DF06B}
AppName=StreamSync
AppVersion={#AppVersion}
AppVerName=StreamSync {#AppVersion}
AppPublisher=StreamSync
AppPublisherURL=https://github.com/gBear97/StreamSync
AppSupportURL=https://github.com/gBear97/StreamSync/issues
AppUpdatesURL=https://github.com/gBear97/StreamSync/releases
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\StreamSync
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename={#OutputBase}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; an update must replace files the running app has open: close it first
CloseApplications=force
RestartApplications=no
UninstallDisplayName=StreamSync
UninstallDisplayIcon={app}\StreamSync.exe

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; A new build can drop libraries the old one shipped. PyInstaller keeps
; them all in _internal, so clear it rather than let a stale DLL load.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\StreamSync"; Filename: "{app}\StreamSync.exe"
Name: "{autodesktop}\StreamSync"; Filename: "{app}\StreamSync.exe"; Tasks: desktopicon

[Run]
; an install by hand offers to start it on the last page
Filename: "{app}\StreamSync.exe"; Description: "{cm:LaunchProgram,StreamSync}"; Flags: nowait postinstall skipifsilent
; an in-app update (silent, /relaunch=1) starts the new version itself
Filename: "{app}\StreamSync.exe"; Flags: nowait; Check: RelaunchAfterUpdate

[Code]
function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|0}') = '1');
end;
