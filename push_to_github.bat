@echo off
rem ---------------------------------------------------------------------------
rem Push this repository to GitHub.
rem
rem Run this ON A MACHINE THAT CAN REACH github.com.  The repository was
rem prepared offline, so the initial commit already exists but has never been
rem pushed.  You need git installed and a way to authenticate:
rem   * HTTPS + Personal Access Token (use the token as the password), or
rem   * SSH (change REMOTE below to git@github.com:...).
rem
rem NOTE: ASCII-only on purpose -- cmd.exe mis-parses UTF-8 batch files.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set REMOTE=https://github.com/Huadiaole/galtext-extractor.git

where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] git not found in PATH.
    pause
    exit /b 1
)

echo === Repository status ===
git log --oneline -1
git status --short
echo.

git remote remove origin >nul 2>&1
git remote add origin %REMOTE%
git branch -M main

echo === Pushing to %REMOTE% ===
echo If prompted for a password, paste a Personal Access Token.
echo.
git push -u origin main
if errorlevel 1 (
    echo.
    echo [FAILED] Push failed. Check the remote URL, your network and credentials.
    echo          If the remote repository does not exist yet, create it on GitHub first
    echo          ^(empty repository, no README^).
    pause
    exit /b 1
)

echo.
echo [OK] Pushed. Suggested next step: tag the release.
echo      git tag -a v1.1.0 -m "GalText Extractor 1.1.0"
echo      git push origin v1.1.0
echo.
echo      Then attach dist\GalTextExtractor.exe and dist\galtext.exe to the
echo      GitHub Release, with docs\RELEASE_NOTES_v1.1.0.md as the description.
echo.
pause
