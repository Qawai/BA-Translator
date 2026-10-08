@echo off
chcp 65001 >nul 2>nul
title Install venv
echo ============================================
echo Installing venv for BA Translator
echo ============================================
if exist venv_win (
    echo venv_win already exists, skipping creation.
) else (
    echo Creating venv_win...
    python -m venv venv_win
    if errorlevel 1 (
        echo ERROR: Failed to create venv_win
        pause
        exit /b 1
    )
    echo venv_win created successfully.
)
echo.
echo Installing dependencies...
venv_win\Scripts\pip.exe install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)
echo.
echo All dependencies installed.
echo.
echo Run Build_all.bat to build distributives.
pause
