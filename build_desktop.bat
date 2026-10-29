@echo off
rem Build the PG Backup Studio desktop app and its installer (Step 9, decision 010).
rem   build_desktop.bat              quick tests, PyInstaller build, installer
rem   build_desktop.bat --no-tests   skip the tests
rem Needs (on this PC only): pip install -r requirements-desktop.txt, and Inno Setup 6.
rem Result: dist\installer\PG Backup Studio Setup <version>.exe
setlocal
cd /d "%~dp0"

for /f "delims=" %%v in ('python -c "import app; print(app.APP_VERSION)"') do set VERSION=%%v
if "%VERSION%"=="" (echo Could not read the version from app.py & exit /b 1)
echo === PG Backup Studio %VERSION%

if /i not "%~1"=="--no-tests" (
  echo === Quick tests
  python -m pytest -q tests\test_step9_desktop.py tests\test_step6_ui.py tests\test_step5_api.py || exit /b 1
)

echo === Icon and version info
python installer\make_icon.py || exit /b 1
python installer\make_version.py || exit /b 1

echo === PyInstaller
python -m PyInstaller --noconfirm --clean pg_backup_studio.spec || exit /b 1

set ISCC=
for %%p in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
  if exist %%p set ISCC=%%p
)
if not defined ISCC (
  echo Inno Setup 6 was not found. Install it with:  winget install JRSoftware.InnoSetup
  exit /b 1
)

echo === Installer
%ISCC% /Q /DAppVersion=%VERSION% installer\pg_backup_studio.iss || exit /b 1
echo.
echo Done: dist\installer\PG Backup Studio Setup %VERSION%.exe
endlocal
