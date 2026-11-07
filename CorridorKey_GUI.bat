@echo off
setlocal EnableExtensions
title CorridorKey 2.0 - Launcher and Setup

REM ============================================================
REM CorridorKey 2.0 - GUI dependency installer + portable launcher
REM Combines:
REM   - TkinterDnD2 drag-and-drop installation
REM   - CorridorKey GUI dependency checks
REM   - OpenColorIO 2.5+ installation and verification
REM   - GUI launch
REM Uses the folder containing this BAT file as the project root.
REM ============================================================

cd /d "%~dp0"
set "ROOT=%~dp0"
set "PYTHON_EXE="
set "PYTHON_ARGS="

echo.
echo ============================================================
echo       CorridorKey 2.0 - Setup and Launcher
echo ============================================================
echo Project folder:
echo %ROOT%
echo.

REM Find a working project virtual environment first.
if exist "%ROOT%venv\Scripts\python.exe" (
    "%ROOT%venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=%ROOT%venv\Scripts\python.exe"
        echo Using project virtual environment.
    ) else (
        echo Existing venv is not usable on this computer.
    )
)

REM Fall back to system Python.
if not defined PYTHON_EXE (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys" >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=python"
            echo Using system Python.
        )
    )
)

REM Final fallback: Windows Python launcher.
if not defined PYTHON_EXE (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -c "import sys" >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=py"
            set "PYTHON_ARGS=-3"
            echo Using Windows Python Launcher.
        )
    )
)

if not defined PYTHON_EXE (
    echo ERROR: Python 3 was not found.
    echo Install Python 3.10-3.13 and enable "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo.
echo Python version:
"%PYTHON_EXE%" %PYTHON_ARGS% --version
echo.

REM Ensure pip is available.
"%PYTHON_EXE%" %PYTHON_ARGS% -m pip --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: pip is not available in this Python environment.
    echo Run: python -m ensurepip --upgrade
    echo.
    pause
    exit /b 1
)

REM Install/check GUI dependencies.
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import customtkinter" >nul 2>&1
if errorlevel 1 (
    echo Installing customtkinter...
    "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install customtkinter
    if errorlevel 1 goto :INSTALL_FAILED
)

"%PYTHON_EXE%" %PYTHON_ARGS% -c "from PIL import Image, ImageTk" >nul 2>&1
if errorlevel 1 (
    echo Installing Pillow...
    "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install Pillow
    if errorlevel 1 goto :INSTALL_FAILED
)

REM NumPy + OpenCV are used for EXR preview and LUT processing.
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import numpy, cv2" >nul 2>&1
if errorlevel 1 (
    echo Installing NumPy and OpenCV...
    "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install numpy opencv-python
    if errorlevel 1 goto :INSTALL_FAILED
)

REM Install TkinterDnD2 for drag-and-drop support.
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import tkinterdnd2" >nul 2>&1
if errorlevel 1 (
    echo Installing TkinterDnD2 for drag-and-drop...
    "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install --upgrade "tkinterdnd2>=0.7.0"
    if errorlevel 1 goto :DND_INSTALL_FAILED
)

REM ACES CG v4 configuration requires OpenColorIO 2.5+.
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import PyOpenColorIO as o; v=tuple(int(x) for x in o.GetVersion().split('.')[:2]); raise SystemExit(0 if v >= (2,5) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Installing/updating OpenColorIO 2.5+...
    "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install --upgrade "opencolorio>=2.5,<2.7"
    if errorlevel 1 goto :OCIO_INSTALL_FAILED
)

REM Verify OpenColorIO after installation.
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import PyOpenColorIO as o; v=tuple(int(x) for x in o.GetVersion().split('.')[:2]); raise SystemExit(0 if v >= (2,5) else 1)" >nul 2>&1
if errorlevel 1 goto :OCIO_INSTALL_FAILED

echo.
echo OpenColorIO version:
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import PyOpenColorIO as o; print(o.GetVersion())"
echo.

REM Confirm drag-and-drop package is importable.
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import tkinterdnd2; print('TkinterDnD2: ready')" 
if errorlevel 1 goto :DND_INSTALL_FAILED

REM Confirm the application script exists.
if not exist "%ROOT%corridorkey_gui.py" (
    echo ERROR: corridorkey_gui.py was not found beside this BAT file.
    echo Expected: %ROOT%corridorkey_gui.py
    echo.
    pause
    exit /b 1
)

echo.
echo All required packages are ready.
echo Starting CorridorKey...
echo.
"%PYTHON_EXE%" %PYTHON_ARGS% -u "%ROOT%corridorkey_gui.py"
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
    echo ============================================================
    echo CorridorKey stopped with error code %ERR%.
    echo ============================================================
    echo.
    pause
) else (
    echo CorridorKey closed normally.
)
endlocal
exit /b %ERR%

:OCIO_INSTALL_FAILED
echo.
echo ERROR: Could not install or verify OpenColorIO 2.5+.
echo Check internet access, then run:
echo python -m pip install --upgrade "opencolorio^>=2.5,^<2.7"
echo.
pause
exit /b 1

:DND_INSTALL_FAILED
echo.
echo ERROR: Could not install or load TkinterDnD2.
echo Check internet access, then run:
echo python -m pip install --upgrade "tkinterdnd2^>=0.7.0"
echo.
pause
exit /b 1

:INSTALL_FAILED
echo.
echo ERROR: Could not install a required GUI package.
echo Check internet access and Python pip configuration.
echo.
pause
exit /b 1

