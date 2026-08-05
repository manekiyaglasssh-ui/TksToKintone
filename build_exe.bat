@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM %~dp0 is an absolute path, including the trailing backslash.  Keep every
REM PyInstaller input rooted here; generated specs live under build\variant.
set "PROJECT_ROOT=%~dp0"
set "PYTHONPATH=%PROJECT_ROOT%;%PYTHONPATH%"
cd /d "%~dp0"

REM ------------------------------------------------------------------
REM Argument parsing.
REM   variant: normal | no-update | no-helper | with-helper | all
REM   --allow-missing-sumatra : dev only, build without installer package
REM ------------------------------------------------------------------
set "REQUESTED_VARIANT="
set "ALLOW_MISSING_SUMATRA=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--allow-missing-sumatra" goto set_allow_flag
if not defined REQUESTED_VARIANT set "REQUESTED_VARIANT=%~1"
shift
goto parse_args

:set_allow_flag
set "ALLOW_MISSING_SUMATRA=1"
shift
goto parse_args

:args_done
if not defined REQUESTED_VARIANT set "REQUESTED_VARIANT=normal"

if /I "%REQUESTED_VARIANT%"=="all" goto build_all

set "BUILD_VARIANT="
if /I "%REQUESTED_VARIANT%"=="normal" set "BUILD_VARIANT=normal"
if /I "%REQUESTED_VARIANT%"=="no-update" set "BUILD_VARIANT=no-update"
if /I "%REQUESTED_VARIANT%"=="no-helper" set "BUILD_VARIANT=no-helper"
if /I "%REQUESTED_VARIANT%"=="with-helper" set "BUILD_VARIANT=with-helper"
if not defined BUILD_VARIANT goto bad_variant

goto check_sumatra


:build_all
set "PASS_FLAG="
if "%ALLOW_MISSING_SUMATRA%"=="1" set "PASS_FLAG=--allow-missing-sumatra"
call "%~f0" normal %PASS_FLAG%
if errorlevel 1 exit /b 1
call "%~f0" no-update %PASS_FLAG%
if errorlevel 1 exit /b 1
echo(
echo All installer variants complete.
exit /b 0


:bad_variant
echo ERROR: unknown build variant "%REQUESTED_VARIANT%".
echo Usage: build_exe.bat [normal^|no-update^|no-helper^|with-helper^|all] [--allow-missing-sumatra]
exit /b 1


REM ------------------------------------------------------------------
REM Fetch and verify the pinned official SumatraPDF installer on the build PC.
REM The target PC never downloads this dependency.
REM ------------------------------------------------------------------
:check_sumatra
set "SKIP_INSTALLER=0"
if "%ALLOW_MISSING_SUMATRA%"=="1" goto sumatra_dev_skip
echo Checking pinned official SumatraPDF installer...
python scripts\download_sumatra.py
if errorlevel 1 goto sumatra_missing
echo SumatraPDF installer size, SHA-256, and PE format verified.
goto sumatra_ok

:sumatra_missing
echo(
echo ============================================================
echo ERROR: verified SumatraPDF installer is missing before build.
echo See scripts\sumatra_config.py for the expected filename and hash.
echo(
echo To build without bundled SumatraPDF for development only:
echo(  build_exe.bat %REQUESTED_VARIANT% --allow-missing-sumatra
echo Run python scripts\download_sumatra.py on a connected build PC.
echo ============================================================
echo(
exit /b 1

:sumatra_dev_skip
echo(
echo WARNING: building WITHOUT the SumatraPDF installer package for development only.
echo(         Installer compile will be skipped. Do NOT use this build for release.
echo(
set "SKIP_INSTALLER=1"
goto sumatra_ok

:sumatra_ok

set "DIST_DIR=%CD%\dist"
set "APP_DIR=%CD%\dist\TksToKintone"
set "VARIANT_DIR=%CD%\build\variant"
set "VARIANT_FILE=%VARIANT_DIR%\build_variant.txt"
echo(
echo Build diagnostics:
echo PROJECT_ROOT=[%PROJECT_ROOT%]
echo VARIANT_DIR=[%VARIANT_DIR%]
echo BUILD_VARIANT=[%BUILD_VARIANT%]

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

python scripts\build_pyinstaller.py normal
if errorlevel 1 (
  echo ERROR: TksToKintone build failed for %BUILD_VARIANT%.
  exit /b 1
)

:build_helper
REM Build the update helper EXE onefile console.
python scripts\build_pyinstaller.py helper
if errorlevel 1 (
  echo ERROR: tks_update_helper build failed.
  exit /b 1
)
if exist "%DIST_DIR%\tks_update_helper.exe" copy /Y "%DIST_DIR%\tks_update_helper.exe" "%APP_DIR%\tks_update_helper.exe"
if errorlevel 1 (
  echo ERROR: failed to copy tks_update_helper.exe into the app folder.
  exit /b 1
)

:after_helper

set "SIGNING_ENABLED=0"
set "SIGN_CERT_MODE="
set "SIGN_CERT_SELECT="
if defined SIGN_CERT_PATH goto use_pfx_certificate
if defined SIGN_CERT_THUMBPRINT goto use_thumbprint_certificate
if defined SIGN_CERT_SUBJECT goto use_subject_certificate
echo Code signing not configured; continuing with an unsigned build.
goto after_sign

:use_thumbprint_certificate
set "SIGN_CERT_MODE=store thumbprint"
set "SIGN_CERT_SELECT=/sha1 "%SIGN_CERT_THUMBPRINT%""
goto signing_configured

:use_subject_certificate
set "SIGN_CERT_MODE=store subject"
set "SIGN_CERT_SELECT=/n "%SIGN_CERT_SUBJECT%""
goto signing_configured

:use_pfx_certificate
if not exist "%SIGN_CERT_PATH%" (
  echo WARNING: SIGN_CERT_PATH does not exist; continuing with an unsigned build.
  goto after_sign
)
set "SIGN_CERT_MODE=PFX"
set "SIGN_CERT_SELECT=/f "%SIGN_CERT_PATH%""
if defined SIGN_CERT_PASSWORD goto use_pfx_password
goto signing_configured

:use_pfx_password
set "SIGN_CERT_SELECT=/f "%SIGN_CERT_PATH%" /p "%SIGN_CERT_PASSWORD%""

:signing_configured
if not defined SIGNTOOL_PATH set "SIGNTOOL_PATH=signtool"
where "%SIGNTOOL_PATH%" >nul 2>nul
if errorlevel 1 (
  if not exist "%SIGNTOOL_PATH%" (
    echo WARNING: signtool was not found; continuing with an unsigned build.
    goto after_sign
  )
)
set "SIGNING_ENABLED=1"
echo Code-signing certificate mode: %SIGN_CERT_MODE%
if /I "%SIGN_CERT_MODE%"=="store subject" echo Code-signing certificate Subject: %SIGN_CERT_SUBJECT%
if /I "%SIGN_CERT_MODE%"=="store thumbprint" echo Code-signing certificate thumbprint: %SIGN_CERT_THUMBPRINT%
echo(
echo Signing EXE: "%APP_DIR%\TksToKintone.exe"
"%SIGNTOOL_PATH%" sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com %SIGN_CERT_SELECT% "%APP_DIR%\TksToKintone.exe"
if errorlevel 1 (
  echo WARNING: signing TksToKintone.exe failed; continuing with an unsigned build.
  set "SIGNING_ENABLED=0"
  goto after_sign
)
echo(
echo Signing helper EXE: "%APP_DIR%\tks_update_helper.exe"
"%SIGNTOOL_PATH%" sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com %SIGN_CERT_SELECT% "%APP_DIR%\tks_update_helper.exe"
if errorlevel 1 (
  echo WARNING: signing tks_update_helper.exe failed; continuing without requiring signatures.
  set "SIGNING_ENABLED=0"
)

:after_sign

REM Verify expected EXE files were created before compiling the installer.
if not exist "%APP_DIR%\TksToKintone.exe" (
  echo ERROR: TksToKintone.exe was not created.
  exit /b 1
)

REM Verify the OLAP request templates were bundled into _internal\docs\olap.
if not exist "%APP_DIR%\_internal\docs\olap\kakou_request_template.json" (
  echo ERROR: kakou_request_template.json missing from bundle _internal\docs\olap.
  exit /b 1
)
if not exist "%APP_DIR%\_internal\docs\olap\soba_request_template.json" (
  echo ERROR: soba_request_template.json missing from bundle _internal\docs\olap.
  exit /b 1
)

:verify_helper_present
if not exist "%APP_DIR%\tks_update_helper.exe" (
  echo ERROR: tks_update_helper.exe was not created.
  exit /b 1
)

:after_verify

if "%SKIP_INSTALLER%"=="1" goto skip_installer

if not defined ISCC_PATH set "ISCC_PATH=ISCC"
echo(
echo Compiling installer variant: %BUILD_VARIANT%
"%ISCC_PATH%" /DMyBuildVariant=%BUILD_VARIANT% installer\tks-to-kintone.iss
if errorlevel 1 (
  echo ERROR: Inno Setup compile failed for %BUILD_VARIANT%.
  exit /b 1
)

REM Code signing is optional. The digest is always produced for update verification.
set "FINAL_SETUP=%CD%\installer\tks-to-kintone-setup.exe"
if /I "%BUILD_VARIANT%"=="no-update" set "FINAL_SETUP=%CD%\installer\tks-to-kintone-setup-no-update.exe"
if /I "%BUILD_VARIANT%"=="no-helper" set "FINAL_SETUP=%CD%\installer\tks-to-kintone-setup-no-helper.exe"
if /I "%BUILD_VARIANT%"=="with-helper" set "FINAL_SETUP=%CD%\installer\tks-to-kintone-setup-with-helper.exe"
if not exist "%FINAL_SETUP%" (
  echo ERROR: compiled Setup EXE was not created.
  exit /b 1
)
if "%SIGNING_ENABLED%"=="1" (
  echo Signing final Setup EXE...
  "%SIGNTOOL_PATH%" sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com %SIGN_CERT_SELECT% "%FINAL_SETUP%"
  if errorlevel 1 echo WARNING: signing final Setup EXE failed; unsigned Setup remains valid.
)
echo Computing SHA-256 for update verification...
python -c "import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.with_suffix(p.suffix+'.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n',encoding='ascii'); print('SHA-256 sidecar created:',p.with_suffix(p.suffix+'.sha256').name)" "%APP_DIR%\TksToKintone.exe"
if errorlevel 1 (
  echo ERROR: TksToKintone.exe SHA-256 creation failed.
  exit /b 1
)
python -c "import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.with_suffix(p.suffix+'.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n',encoding='ascii'); print('SHA-256 sidecar created:',p.with_suffix(p.suffix+'.sha256').name)" "%APP_DIR%\tks_update_helper.exe"
if errorlevel 1 (
  echo ERROR: tks_update_helper.exe SHA-256 creation failed.
  exit /b 1
)
python -c "import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.with_suffix(p.suffix+'.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n',encoding='ascii'); print('SHA-256 sidecar created:',p.with_suffix(p.suffix+'.sha256').name)" "%FINAL_SETUP%"
if errorlevel 1 (
  echo ERROR: SHA-256 creation failed.
  exit /b 1
)
goto summary

:skip_installer
echo(
echo Skipping installer compile - SumatraPDF installer package unavailable, dev build.
echo App EXE only: "%APP_DIR%\TksToKintone.exe"
echo Build complete without installer: %BUILD_VARIANT%
exit /b 0

:summary
echo(
echo Build complete: %BUILD_VARIANT%
echo MAIN EXE: "%APP_DIR%\TksToKintone.exe"
if /I "%BUILD_VARIANT%"=="with-helper" echo HELPER EXE: "%APP_DIR%\tks_update_helper.exe"
echo INSTALLER_DIR: "%CD%\installer"
echo APP_DIR: "%APP_DIR%"
echo(
echo Generated files:
dir /b "%APP_DIR%"
exit /b 0
