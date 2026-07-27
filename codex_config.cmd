@echo off

rem Shared defaults. Override machine-specific values in codex_config.local.cmd.
if not defined CODEX_PORTABLE_TARGET set "CODEX_PORTABLE_TARGET=D:\PortableApp\CodexPortable"
if not defined CODEX_PROXY set "CODEX_PROXY=http://127.0.0.1:7890"

if exist "%~dp0codex_config.local.cmd" call "%~dp0codex_config.local.cmd"

if not defined CODEX_PYTHON_EXE if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" (
    set "CODEX_PYTHON_EXE=%CONDA_PREFIX%\python.exe"
)
if not defined CODEX_PYTHON_EXE if exist "%USERPROFILE%\miniconda3\python.exe" (
    set "CODEX_PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"
)
if not defined CODEX_PYTHON_EXE if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "CODEX_PYTHON_EXE=%USERPROFILE%\anaconda3\python.exe"
)
