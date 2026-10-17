; Inno Setup script: builds a one-click Windows installer from dist\CryptoBot (PyInstaller output).
; Install Inno Setup 6 (https://jrsoftware.org/isinfo.php), then:  iscc packaging\installer.iss
#define AppName "CryptoBot"
#define AppVersion GetEnv("CRYPTOBOT_VERSION")
#if AppVersion == ""
#define AppVersion "0.1.0"
#endif

[Setup]
AppId={{7D3C1A4E-9B0F-4C5A-8E2D-CRYPTOBOT0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=CryptoBot
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=CryptoBot-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\CryptoBot.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\CryptoBot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\CryptoBot.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\CryptoBot.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\CryptoBot.exe"; Description: "Launch CryptoBot"; Flags: nowait postinstall skipifsilent
