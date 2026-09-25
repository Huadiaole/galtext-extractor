@echo off
rem GalText Extractor command-line entry: forwards all arguments to python -m galtext
rem Example: galtext.bat scan "D:\Game" -o out.csv -f csv
rem NOTE: ASCII-only on purpose -- cmd.exe mis-parses UTF-8 batch files.
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1
python -m galtext %*
endlocal
