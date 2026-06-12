@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
  py -3.11 -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

pyinstaller ^
  --noconfirm ^
  --onedir ^
  --windowed ^
  --name TksToKintone ^
  --icon assets\app_icon.ico ^
  --add-data "templates;templates" ^
  --add-data "docs\olap;docs\olap" ^
  --add-data "assets;assets" ^
  app\main.py

if defined SIGN_CERT_PATH (
  if not defined SIGNTOOL_PATH set "SIGNTOOL_PATH=signtool"
  echo.
  echo Signing EXE: "%CD%\dist\TksToKintone\TksToKintone.exe"
  "%SIGNTOOL_PATH%" sign ^
    /fd SHA256 ^
    /td SHA256 ^
    /tr http://timestamp.digicert.com ^
    /f "%SIGN_CERT_PATH%" ^
    /p "%SIGN_CERT_PASSWORD%" ^
    "%CD%\dist\TksToKintone\TksToKintone.exe"
)

echo.
echo Build complete.
echo EXE: "%CD%\dist\TksToKintone\TksToKintone.exe"
echo DIST_DIR: "%CD%\dist"
echo APP_DIR: "%CD%\dist\TksToKintone"
if exist "%CD%\dist\TksToKintone" (
  echo.
  echo Generated files:
  dir /b "%CD%\dist\TksToKintone"
)
