@echo off
setlocal
cd /d "%~dp0"

rem Browser Backup Manager - one-command build.
rem Result: dist\BrowserBackupManager.exe (single portable file)

where py >nul 2>nul && (set "PY=py") || (set "PY=python")
%PY% --version >nul 2>nul || (echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & goto :fail)

echo Installing build requirements...
%PY% -m pip install -r requirements.txt pyinstaller || goto :fail

echo Building the EXE...
%PY% -m PyInstaller --noconfirm --clean BrowserBackupManager.spec || goto :fail

echo.
echo Done: %~dp0dist\BrowserBackupManager.exe
if "%1"=="" pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
if "%1"=="" pause
exit /b 1
