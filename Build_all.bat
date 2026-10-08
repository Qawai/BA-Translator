@echo off
chcp 65001 >nul 2>nul
title Build BA Translator
echo ============================================================
echo          BUILD ALL - BA Translator
echo ============================================================
echo.

if not exist venv_win (
    echo ERROR: venv_win not found. Run Install_venv.bat first.
    pause
    exit /b 1
)

echo [=>              ] Checking pyinstaller...
venv_win\Scripts\pip.exe show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo Installing pyinstaller...
    venv_win\Scripts\pip.exe install pyinstaller
)

echo [====>           ] Building Windows executable...

venv_win\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --icon NONE --name Translator_for_BA_win --add-data "data;data" Translator_for_BA.py
if errorlevel 1 (
    echo ERROR: Windows build failed!
    pause
    exit /b 1
)

echo [========>       ] Creating launcher script...

echo @echo off > "dist\Translator_for_BA_win.bat"
echo cd /d "%%~dp0" >> "dist\Translator_for_BA_win.bat"
echo start "" "Translator_for_BA_win.exe" >> "dist\Translator_for_BA_win.bat"

echo [================] BUILD COMPLETE!
echo.
echo Output files in dist\:
dir dist\ /b
echo.
pause
