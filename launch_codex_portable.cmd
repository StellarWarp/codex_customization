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

title Launch Codex Portable

set "CODEX_PORTABLE_CHECK_ONLY=0"
if /I "%~1"=="--check-only" set "CODEX_PORTABLE_CHECK_ONLY=1"
if not "%~1"=="" if /I not "%~1"=="--check-only" goto :usage

if not exist "%CODEX_PORTABLE_TARGET%\ChatGPT.exe" (
    echo ERROR: Codex Portable launcher was not found:
    echo   %CODEX_PORTABLE_TARGET%\ChatGPT.exe
    goto :failed
)

echo Checking proxy: %CODEX_PROXY%
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $uri=[Uri]$env:CODEX_PROXY; if($uri.Scheme -notin @('http','https')){throw 'CODEX_PROXY must use http or https.'}; $client=[Net.Sockets.TcpClient]::new(); try{$task=$client.ConnectAsync($uri.Host,$uri.Port); if(-not $task.Wait(1000) -or -not $client.Connected){throw ('Proxy is not listening at {0}:{1}' -f $uri.Host,$uri.Port)}} finally{$client.Dispose()}"
if errorlevel 1 goto :failed

set "HTTP_PROXY=%CODEX_PROXY%"
set "HTTPS_PROXY=%CODEX_PROXY%"
set "ALL_PROXY=%CODEX_PROXY%"
set "NO_PROXY=localhost,127.0.0.1,::1"

echo Target: %CODEX_PORTABLE_TARGET%
echo Launching with the OpenAI.Codex package identity...
rem The portable copy lives outside the MSIX package, so Windows refuses to start
rem it without package identity ("no package identity" bootstrap failure). The
rem registered OpenAI.Codex package supplies that identity. Environment variables
rem are not inherited by the packaged child, so the app uses the Windows user
rem proxy setting instead of HTTP_PROXY/HTTPS_PROXY.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $exe=Join-Path $env:CODEX_PORTABLE_TARGET 'ChatGPT.exe'; if(-not (Test-Path -LiteralPath $exe -PathType Leaf)){throw ('The portable executable was not found: '+$exe)}; $pkg=Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1; if($null -ne $pkg){$installLocation=$pkg.InstallLocation; $packageFamilyName=$pkg.PackageFamilyName; $packageFullName=$pkg.PackageFullName}else{$installLocation=$env:CODEX_PACKAGE_ROOT; if([string]::IsNullOrWhiteSpace($installLocation) -or -not (Test-Path -LiteralPath $installLocation -PathType Container)){throw 'OpenAI.Codex is not registered and CODEX_PACKAGE_ROOT is unavailable.'}; $packageFullName=Split-Path -Leaf $installLocation; if($packageFullName -notmatch '^(?<name>.+?)_.+__(?<publisherId>[^_]+)$'){throw 'CODEX_PACKAGE_ROOT does not have a recognized MSIX package directory name.'}; $packageFamilyName=$Matches.name+'_'+$Matches.publisherId}; $manifestPath=Join-Path $installLocation 'AppxManifest.xml'; [xml]$manifest=Get-Content -LiteralPath $manifestPath -Raw; $apps=@($manifest.Package.Applications.Application); $app=$apps | Where-Object { $_.Executable -match '(?i)(^|[\\/])ChatGPT\.exe$' } | Select-Object -First 1; if($null -eq $app){$app=$apps | Select-Object -First 1}; if($null -eq $app -or [string]::IsNullOrWhiteSpace([string]$app.Id)){throw 'The Codex application entry was not found in AppxManifest.xml.'}; $identity='{0}!{1}' -f $packageFamilyName,$app.Id; Write-Host ('Package: {0}' -f $packageFullName); Write-Host ('AppUserModelId: {0}' -f $identity); Write-Host ('Portable executable: {0}' -f $exe); $proxy=Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue; if($proxy.ProxyEnable -ne 1){Write-Warning ('The Windows user proxy is disabled, so the packaged child cannot use {0} through HTTP_PROXY.' -f $env:CODEX_PROXY)}else{Write-Host ('Windows user proxy: {0}' -f $proxy.ProxyServer)}; if($env:CODEX_PORTABLE_CHECK_ONLY -eq '1'){exit 0}; if($null -eq (Get-Command Invoke-CommandInDesktopPackage -ErrorAction SilentlyContinue)){throw 'Invoke-CommandInDesktopPackage is unavailable; the portable copy needs the OpenAI.Codex package identity to start.'}; Invoke-CommandInDesktopPackage -PackageFamilyName $packageFamilyName -AppId ([string]$app.Id) -Command $exe -PreventBreakaway; Write-Host 'Codex Portable launch request sent with package identity.'"
if errorlevel 1 goto :failed

if "%CODEX_PORTABLE_CHECK_ONLY%"=="1" (
    echo Codex Portable, its proxy, and its package identity are available.
) else (
    echo Codex Portable launch request sent with the OpenAI.Codex package identity.
)
exit /b 0

:usage
echo Usage: %~nx0 [--check-only]
exit /b 2

:failed
echo.
echo ERROR: Could not launch Codex Portable through the configured proxy.
echo Install or repair the OpenAI.Codex package so the portable copy can borrow its identity.
pause
exit /b 1
