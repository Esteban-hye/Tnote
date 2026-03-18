@echo off
title TNote - Installation

python --version >nul 2>&1 || (echo [ERREUR] Python absent & pause & exit /b 1)

for /f "tokens=*" %%P in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%P"
echo [1/3] dependances...
pip install pystray Pillow || (echo [ERREUR] Echec pip & pause & exit /b 1)
echo [2/3] Ajout au demarrage
set "VBS=%TEMP%\tnote.vbs"
(
    echo Set W=CreateObject^("WScript.Shell"^)
    echo Set S=W.CreateShortcut^("%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TNote.lnk"^)
    echo S.TargetPath="%PYW%"
    echo S.Arguments="""%~dp0tnote.py"""
    echo S.WorkingDirectory="%~dp0"
    echo S.IconLocation="shell32.dll,70"
    echo S.Save
) > "%VBS%"
cscript //nologo "%VBS%" & del "%VBS%"

echo [3/3] Lancement...
start "" "%PYW%" "%~dp0tnote.py"

echo Fini
pause