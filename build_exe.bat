@echo off
rem Build a single-file executable (downloads PyInstaller, needs network).
rem NOTE: ASCII-only on purpose -- cmd.exe mis-parses UTF-8 batch files.
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    pause
    exit /b 1
)

if not exist ".venv-build\Scripts\python.exe" (
    echo [1/4] Creating build virtualenv .venv-build ...
    python -m venv .venv-build || goto :fail
)

echo [2/4] Installing PyInstaller ...
".venv-build\Scripts\python.exe" -m pip install --upgrade pip --quiet || goto :fail
".venv-build\Scripts\python.exe" -m pip install pyinstaller --quiet || goto :fail

echo [3/4] Building GUI executable (windowed) ...
rem --collect-submodules pulls in the dynamically imported parsers;
rem --collect-data is ALSO required, otherwise galtext/assets/** (the app icon
rem and toolbar icons) is left out and the packaged exe ships without artwork.
".venv-build\Scripts\python.exe" -m PyInstaller ^
    --noconfirm --clean --onefile --windowed ^
    --name GalTextExtractor ^
    --distpath dist ^
    --workpath build\gui ^
    --specpath build ^
    --collect-submodules galtext ^
    --collect-data galtext ^
    --icon "galtext\assets\app.ico" ^
    --hidden-import tkinter ^
    --hidden-import tkinter.ttk ^
    --hidden-import tkinter.filedialog ^
    --hidden-import tkinter.messagebox ^
    entry_point.py || goto :fail

echo [4/4] Building CLI executable (console) ...
rem A --windowed build has no stdout, so it cannot serve as a CLI.
rem Ship a second, console build for "galtext.exe engines / scan ...".
".venv-build\Scripts\python.exe" -m PyInstaller ^
    --noconfirm --onefile --console ^
    --name galtext ^
    --distpath dist ^
    --workpath build\cli ^
    --specpath build ^
    --collect-submodules galtext ^
    --collect-data galtext ^
    entry_point.py || goto :fail

echo Done.
echo.
echo Output:
echo   %CD%\dist\GalTextExtractor.exe   (GUI, double-click)
echo   %CD%\dist\galtext.exe            (CLI, e.g. galtext.exe scan "D:\Game" -f pdf)
echo.
pause
exit /b 0

:fail
echo.
echo [FAILED] Packaging error, see output above.
pause
exit /b 1
