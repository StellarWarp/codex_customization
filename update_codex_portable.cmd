@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "CONFIG_SCRIPT=%SCRIPT_DIR%codex_config.cmd"
set "PATCH_SCRIPT=%SCRIPT_DIR%patch_codex_model_experience_copy.py"
set "PORTABLE_LAUNCHER=%SCRIPT_DIR%launch_codex_portable.cmd"
if not exist "%CONFIG_SCRIPT%" (
    echo ERROR: Shared configuration was not found:
    echo   %CONFIG_SCRIPT%
    goto :failed
)
call "%CONFIG_SCRIPT%"
if errorlevel 1 goto :failed

set "PYTHON_EXE=%CODEX_PYTHON_EXE%"
set "PATCH_OPTIONS="
if "%CODEX_ENABLE_REMOTE_CONTROL_PATCH%"=="1" set "PATCH_OPTIONS=--enable-control-other-devices"

title Update Codex Portable

if not exist "%PATCH_SCRIPT%" (
    echo ERROR: Patch script not found:
    echo   %PATCH_SCRIPT%
    goto :failed
)
if not exist "%PORTABLE_LAUNCHER%" (
    echo ERROR: Portable launcher script not found:
    echo   %PORTABLE_LAUNCHER%
    goto :failed
)

if not defined PYTHON_EXE (
    echo ERROR: A Conda Python interpreter was not found.
    echo Configure CODEX_PYTHON_EXE in codex_config.local.cmd.
    goto :failed
)

echo Target: %CODEX_PORTABLE_TARGET%
echo Python: %PYTHON_EXE%
echo Control other devices patch: %CODEX_ENABLE_REMOTE_CONTROL_PATCH%

cd /d "%SCRIPT_DIR%"

echo.
echo Running read-only compatibility check...
"%PYTHON_EXE%" -B "%PATCH_SCRIPT%" %PATCH_OPTIONS%
if errorlevel 1 (
    echo ERROR: Compatibility check failed. The existing portable copy was not changed.
    goto :failed
)
if /I "%~1"=="--check-only" (
    echo Check-only mode completed successfully.
    exit /b 0
)

echo.
echo Stopping all processes running from the target directory...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $root=[IO.Path]::GetFullPath($env:CODEX_PORTABLE_TARGET).TrimEnd('\')+'\'; for($attempt=0;$attempt -lt 10;$attempt++){ $matches=@(Get-Process -ErrorAction SilentlyContinue | Where-Object { try { $path=$_.Path } catch { $path=$null }; $path -and $path.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) }); if($matches.Count -eq 0){exit 0}; $matches | Stop-Process -Force -ErrorAction Stop; Start-Sleep -Milliseconds 250 }; throw 'Portable processes are still running after 2.5 seconds.' } catch { Write-Error $_; exit 1 }"
if errorlevel 1 (
    echo ERROR: Could not stop the existing portable processes.
    goto :failed
)
if /I "%~1"=="--check-stop-only" (
    echo Stop-command check completed successfully.
    exit /b 0
)

echo.
echo Building and patching the new portable copy...
echo The existing portable copy will be deleted after the staged copy is verified.
"%PYTHON_EXE%" -B "%PATCH_SCRIPT%" %PATCH_OPTIONS% --apply --replace-output --output-dir "%CODEX_PORTABLE_TARGET%"
if errorlevel 1 (
    echo ERROR: Portable update failed. Review the output above.
    goto :failed
)

if not exist "%CODEX_PORTABLE_TARGET%\ChatGPT.exe" (
    echo ERROR: Updated launcher was not found.
    goto :failed
)

echo.
echo Update completed successfully. Launching Codex Portable...
call "%PORTABLE_LAUNCHER%"
if errorlevel 1 (
    echo ERROR: Portable update succeeded, but the proxied launch failed.
    goto :failed
)
exit /b 0

:failed
echo.
pause
exit /b 1
