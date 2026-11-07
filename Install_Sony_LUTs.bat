@echo off
setlocal EnableExtensions
title CorridorKey - Sony LUT Installer

REM ============================================================
REM CorridorKey Sony LUT installer - PATH QUOTING FIX
REM ROOT is canonicalized WITHOUT a trailing backslash. This avoids
REM Windows passing a literal quote into Python's --root argument.
REM ============================================================

for %%I in ("%~dp0.") do set "ROOT=%%~fI"
cd /d "%ROOT%" || (
    echo ERROR: Cannot access the CorridorKey project folder:
    echo %ROOT%
    if not defined CORRIDORKEY_GUI_LAUNCH pause
    exit /b 1
)

set "PY=%ROOT%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "DOWNLOADER=%ROOT%\sony_lut_download.py"
set "LUT_DIR=%ROOT%\resources\luts\sony"
set "LOG_DIR=%ROOT%\logs"

echo.
echo ============================================================
echo       CorridorKey - Automatic Sony LUT Installer
echo ============================================================
echo Project: %ROOT%
echo Python:  %PY%
echo LUTs:    %LUT_DIR%
echo ============================================================
echo.

if not exist "%LUT_DIR%\" mkdir "%LUT_DIR%" 2>nul
if not exist "%LOG_DIR%\" mkdir "%LOG_DIR%" 2>nul

if not exist "%DOWNLOADER%" (
    echo ERROR: sony_lut_download.py was not found:
    echo %DOWNLOADER%
    if not defined CORRIDORKEY_GUI_LAUNCH pause
    exit /b 2
)

REM Diagnostic: print the path exactly as cmd will pass it.
echo Running downloader with project root:
echo [%ROOT%]
echo.

"%PY%" -u "%DOWNLOADER%" --root "%ROOT%"
set "RESULT=%ERRORLEVEL%"

echo.
if "%RESULT%"=="0" (
    echo Sony LUT installation completed.
    echo Installed files: %LUT_DIR%
) else (
    echo Sony LUT installation failed with exit code %RESULT%.
    echo Installer log: %LOG_DIR%\sony_lut_install.log
)

if not defined CORRIDORKEY_GUI_LAUNCH pause
exit /b %RESULT%
