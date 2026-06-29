@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "REQUESTED_VARIANT=%~1"
if "%REQUESTED_VARIANT%"=="" set "REQUESTED_VARIANT=normal"

if /I "%REQUESTED_VARIANT%"=="all" (
  call "%~f0" normal
  if errorlevel 1 exit /b 1
  call "%~f0" no-update
  if errorlevel 1 exit /b 1
  echo.
  echo All installer variants complete.
  exit /b 0
)

set "BUILD_VARIANT="
if /I "%REQUESTED_VARIANT%"=="normal" set "BUILD_VARIANT=normal"
if /I "%REQUESTED_VARIANT%"=="no-update" set "BUILD_VARIANT=no-update"
if /I "%REQUESTED_VARIANT%"=="no-helper" set "BUILD_VARIANT=no-helper"
if /I "%REQUESTED_VARIANT%"=="with-helper" set "BUILD_VARIANT=with-helper"
if "%BUILD_VARIANT%"=="" (
  echo ERROR: unknown build variant "%REQUESTED_VARIANT%".
  echo Usage: build_exe.bat [normal^|no-update^|no-helper^|with-helper^|all]
  exit /b 1
)

set "DIST_DIR=%CD%\dist"
set "APP_DIR=%CD%\dist\TksToKintone"
set "VARIANT_DIR=%CD%\build\variant"
set "VARIANT_FILE=%VARIANT_DIR%\build_variant.txt"

if not exist .venv (
  py -3.11 -m venv .venv
)

call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
if errorlevel 1 (
  echo ERROR: pip upgrade failed.
  exit /b 1
)

pip install -r requirements.txt
if errorlevel 1 (
  echo ERROR: requirements install failed.
  exit /b 1
)

if exist "%APP_DIR%" rmdir /S /Q "%APP_DIR%"
if not exist "%VARIANT_DIR%" mkdir "%VARIANT_DIR%"
> "%VARIANT_FILE%" echo %BUILD_VARIANT%

set "PYINSTALLER_UPDATE_ARGS=--hidden-import app.update_client"
if /I "%BUILD_VARIANT%"=="no-update" (
  set "PYINSTALLER_UPDATE_ARGS=--exclude-module app.update_client --exclude-module app.update_helper"
)

REM Build the main application EXE (onedir, windowed). Keep the command on one line.
python -m PyInstaller --noconfirm --clean --onedir --windowed --name TksToKintone --icon assets\app_icon.ico --add-data "templates;templates" --add-data "docs\olap;docs\olap" --add-data "assets;assets" --add-data "%VARIANT_FILE%;." %PYINSTALLER_UPDATE_ARGS% app\main.py
if errorlevel 1 (
  echo ERROR: TksToKintone build failed for %BUILD_VARIANT%.
  exit /b 1
)

if /I "%BUILD_VARIANT%"=="with-helper" (
  REM Build the update helper EXE (onefile, console). This avoids using PowerShell during updates.
  python -m PyInstaller --noconfirm --clean --onefile --console --name tks_update_helper app\update_helper.py
  if errorlevel 1 (
    echo ERROR: tks_update_helper build failed.
    exit /b 1
  )

  REM Place the helper EXE next to the main EXE so the installer bundles it.
  if exist "%DIST_DIR%\tks_update_helper.exe" (
    copy /Y "%DIST_DIR%\tks_update_helper.exe" "%APP_DIR%\tks_update_helper.exe"
  )
  if errorlevel 1 (
    echo ERROR: failed to copy tks_update_helper.exe into the app folder.
    exit /b 1
  )
) else (
  REM Official builds do not include the old helper EXE.
  if exist "%DIST_DIR%\tks_update_helper.exe" del /F /Q "%DIST_DIR%\tks_update_helper.exe"
  if exist "%APP_DIR%\tks_update_helper.exe" del /F /Q "%APP_DIR%\tks_update_helper.exe"
)

if defined SIGN_CERT_PATH (
  if not defined SIGNTOOL_PATH set "SIGNTOOL_PATH=signtool"
  echo.
  echo Signing EXE: "%APP_DIR%\TksToKintone.exe"
  "%SIGNTOOL_PATH%" sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /f "%SIGN_CERT_PATH%" /p "%SIGN_CERT_PASSWORD%" "%APP_DIR%\TksToKintone.exe"
  if errorlevel 1 (
    echo ERROR: signing TksToKintone.exe failed.
    exit /b 1
  )
  if /I "%BUILD_VARIANT%"=="with-helper" (
    echo.
    echo Signing helper EXE: "%APP_DIR%\tks_update_helper.exe"
    "%SIGNTOOL_PATH%" sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /f "%SIGN_CERT_PATH%" /p "%SIGN_CERT_PASSWORD%" "%APP_DIR%\tks_update_helper.exe"
    if errorlevel 1 (
      echo ERROR: signing tks_update_helper.exe failed.
      exit /b 1
    )
  )
)

REM Verify expected EXE files were created before compiling the installer.
if not exist "%APP_DIR%\TksToKintone.exe" (
  echo ERROR: TksToKintone.exe was not created.
  exit /b 1
)
if /I "%BUILD_VARIANT%"=="with-helper" (
  if not exist "%APP_DIR%\tks_update_helper.exe" (
    echo ERROR: tks_update_helper.exe was not created.
    exit /b 1
  )
) else (
  if exist "%APP_DIR%\tks_update_helper.exe" (
    echo ERROR: tks_update_helper.exe must not be bundled for %BUILD_VARIANT%.
    exit /b 1
  )
)

if not defined ISCC_PATH set "ISCC_PATH=ISCC"
echo.
echo Compiling installer variant: %BUILD_VARIANT%
"%ISCC_PATH%" /DMyBuildVariant=%BUILD_VARIANT% installer\tks-to-kintone.iss
if errorlevel 1 (
  echo ERROR: Inno Setup compile failed for %BUILD_VARIANT%.
  exit /b 1
)

echo.
echo Build complete: %BUILD_VARIANT%
echo MAIN EXE: "%APP_DIR%\TksToKintone.exe"
if /I "%BUILD_VARIANT%"=="with-helper" echo HELPER EXE: "%APP_DIR%\tks_update_helper.exe"
echo INSTALLER_DIR: "%CD%\installer"
echo APP_DIR: "%APP_DIR%"
echo.
echo Generated files:
dir /b "%APP_DIR%"
