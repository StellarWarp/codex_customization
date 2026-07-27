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

title Launch Original Codex

set "CODEX_ORIGINAL_CHECK_ONLY=0"
if /I "%~1"=="--check-only" set "CODEX_ORIGINAL_CHECK_ONLY=1"
if not "%~1"=="" if /I not "%~1"=="--check-only" goto :usage

echo Checking proxy: %CODEX_PROXY%
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $uri=[Uri]$env:CODEX_PROXY; if($uri.Scheme -notin @('http','https')){throw 'CODEX_PROXY must use http or https.'}; $client=[Net.Sockets.TcpClient]::new(); try{$task=$client.ConnectAsync($uri.Host,$uri.Port); if(-not $task.Wait(1000) -or -not $client.Connected){throw ('Proxy is not listening at {0}:{1}' -f $uri.Host,$uri.Port)}} finally{$client.Dispose()}"
if errorlevel 1 goto :failed

set "HTTP_PROXY=%CODEX_PROXY%"
set "HTTPS_PROXY=%CODEX_PROXY%"
set "ALL_PROXY=%CODEX_PROXY%"
set "NO_PROXY=localhost,127.0.0.1,::1"

echo Locating the registered OpenAI.Codex package...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $pkg=Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1; if($null -eq $pkg){throw 'OpenAI.Codex is not registered for the current Windows user.'}; $manifestPath=Join-Path $pkg.InstallLocation 'AppxManifest.xml'; [xml]$manifest=Get-Content -LiteralPath $manifestPath -Raw; $apps=@($manifest.Package.Applications.Application); $app=$apps | Where-Object { $_.Executable -match '(?i)(^|[\\/])ChatGPT\.exe$' } | Select-Object -First 1; if($null -eq $app){$app=$apps | Select-Object -First 1}; if($null -eq $app -or [string]::IsNullOrWhiteSpace([string]$app.Id)){throw 'The Codex application entry was not found in AppxManifest.xml.'}; $aumid='{0}!{1}' -f $pkg.PackageFamilyName,$app.Id; Write-Host ('Package: {0}' -f $pkg.PackageFullName); Write-Host ('AppUserModelId: {0}' -f $aumid); if($env:CODEX_ORIGINAL_CHECK_ONLY -eq '1'){exit 0}; Start-Process -FilePath 'explorer.exe' -ArgumentList ('shell:AppsFolder\{0}' -f $aumid)"
if errorlevel 1 goto :failed

if "%CODEX_ORIGINAL_CHECK_ONLY%"=="1" (
    echo Original Codex launch entry is available.
) else (
    echo Original Codex launch request sent.
)
exit /b 0

:usage
echo Usage: %~nx0 [--check-only]
exit /b 2

:failed
echo.
echo ERROR: Could not launch the registered original Codex app through the configured proxy.
echo Check the proxy, or install or repair OpenAI Codex for this Windows user.
pause
exit /b 1
