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
echo HTTP_PROXY: %HTTP_PROXY%

if /I "%~1"=="--check-only" (
    echo Codex Portable and its proxy are available.
    exit /b 0
)

start "" /D "%CODEX_PORTABLE_TARGET%" "%CODEX_PORTABLE_TARGET%\ChatGPT.exe"
if errorlevel 1 goto :failed

echo Codex Portable launch request sent with proxy variables.
exit /b 0

:usage
echo Usage: %~nx0 [--check-only]
exit /b 2

:failed
echo.
echo ERROR: Could not launch Codex Portable through the configured proxy.
pause
exit /b 1
