@echo off
chcp 65001 >nul 2>nul
title Debug Build
echo ============================================
echo   DEBUG BUILD - BA Translator
echo ============================================
echo.

if not exist venv_win (
    echo ERROR: venv_win not found.
    pause
    exit /b 1
)

echo [=>              ] Running debug...
venv_win\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name Translator_for_BA_win --add-data "data;data" Translator_for_BA.py 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Build failed!
) else (
    echo.
    echo Build successful.
)

echo.
pause
