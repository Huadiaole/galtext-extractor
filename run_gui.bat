@echo off
rem Launch the GalText Extractor graphical interface.
rem NOTE: this file is intentionally ASCII-only -- cmd.exe mis-parses
rem UTF-8 batch files. All localized text is produced by Python.
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)

python -m galtext gui %*
if errorlevel 1 (
    echo.
    echo [HINT] GUI failed to start. You can use the command line instead:
    echo        python -m galtext scan "GAME_DIR" -o out.csv -f csv
    pause
)
endlocal
