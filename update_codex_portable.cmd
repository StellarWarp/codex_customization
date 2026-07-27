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

cd /d "%SCRIPT_DIR%"

echo.
echo Running read-only compatibility check...
"%PYTHON_EXE%" -B "%PATCH_SCRIPT%"
if errorlevel 1 (
    echo ERROR: Compatibility check failed. The existing portable copy was not changed.
    goto :failed
)
if /I "%~1"=="--check-only" (
    echo Check-only mode completed successfully.
    exit /b 0
)

echo.
echo Stopping only Codex Desktop processes running from the target directory...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $root=[IO.Path]::GetFullPath($env:CODEX_PORTABLE_TARGET).TrimEnd('\')+'\'; Get-Process -Name ChatGPT,codex -ErrorAction SilentlyContinue | ForEach-Object { try { $path=$_.Path } catch { $path=$null }; if ($path -and $path.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)) { Stop-Process -Id $_.Id -Force -ErrorAction Stop } }; exit 0 } catch { Write-Error $_; exit 1 }"
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
"%PYTHON_EXE%" -B "%PATCH_SCRIPT%" --apply --replace-output --output-dir "%CODEX_PORTABLE_TARGET%"
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
