@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "CONFIG_SCRIPT=%SCRIPT_DIR%codex_config.cmd"
if not exist "%CONFIG_SCRIPT%" (
    echo ERROR: Shared configuration was not found:
    echo   %CONFIG_SCRIPT%
    goto :failed
)
call "%CONFIG_SCRIPT%"
if errorlevel 1 goto :failed

set "ORIGINAL_SCRIPT=%SCRIPT_DIR%launch_original_codex.cmd"
set "PORTABLE_SCRIPT=%SCRIPT_DIR%launch_codex_portable.cmd"
set "SHORTCUT_MODE=install"
if /I "%~1"=="--remove" set "SHORTCUT_MODE=remove"
if not "%~1"=="" if /I not "%~1"=="--remove" goto :usage

if not exist "%ORIGINAL_SCRIPT%" (
    echo ERROR: Original Codex launcher was not found:
    echo   %ORIGINAL_SCRIPT%
    goto :failed
)
if not exist "%PORTABLE_SCRIPT%" (
    echo ERROR: Portable Codex launcher was not found:
    echo   %PORTABLE_SCRIPT%
    goto :failed
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $menu=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Codex'; $names=@('Codex Original.lnk','Codex Custom.lnk','Codex Portable.lnk'); if($env:SHORTCUT_MODE -eq 'remove'){foreach($name in $names){$path=Join-Path $menu $name; if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force}}; if((Test-Path -LiteralPath $menu -PathType Container) -and @(Get-ChildItem -LiteralPath $menu -Force).Count -eq 0){Remove-Item -LiteralPath $menu -Force}; Write-Host 'Codex Start Menu shortcuts removed.'; exit 0}; New-Item -ItemType Directory -Force -Path $menu | Out-Null; $legacy=Join-Path $menu 'Codex Portable.lnk'; if(Test-Path -LiteralPath $legacy){Remove-Item -LiteralPath $legacy -Force}; $shell=New-Object -ComObject WScript.Shell; $portableIcon=Join-Path $env:CODEX_PORTABLE_TARGET 'ChatGPT.exe'; $icon=if(Test-Path -LiteralPath $portableIcon -PathType Leaf){$portableIcon+',0'}else{$env:SystemRoot+'\System32\SHELL32.dll,220'}; $items=@(@{Name='Codex Original.lnk';Script=$env:ORIGINAL_SCRIPT;Description='Launch the original Codex Desktop app through the configured proxy.'},@{Name='Codex Custom.lnk';Script=$env:PORTABLE_SCRIPT;Description='Launch the custom portable Codex app through the configured proxy.'}); foreach($item in $items){$shortcut=$shell.CreateShortcut((Join-Path $menu $item.Name)); $shortcut.TargetPath=$item.Script; $shortcut.Arguments=''; $shortcut.WorkingDirectory=Split-Path -Parent $item.Script; $shortcut.Description=$item.Description; $shortcut.IconLocation=$icon; $shortcut.Save()}; Write-Host ('Created Start Menu shortcuts in: '+$menu); Write-Host ('Icon source: '+$icon)"
if errorlevel 1 goto :failed

if "%SHORTCUT_MODE%"=="remove" (
    echo Done.
) else (
    echo Done. Open Start Menu and search for "Codex Original" or "Codex Custom".
)
exit /b 0

:usage
echo Usage: %~nx0 [--remove]
exit /b 2

:failed
echo.
echo ERROR: Could not update the Codex Start Menu shortcuts.
pause
exit /b 1
