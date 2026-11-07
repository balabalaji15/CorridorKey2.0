@echo off
setlocal
cd /d "%~dp0"
echo Installing TkinterDnD2 for CorridorKey...
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -m pip install --upgrade "tkinterdnd2>=0.7.0"
) else (
    python -m pip install --upgrade "tkinterdnd2>=0.7.0"
)
if errorlevel 1 (
    echo.
    echo Installation failed. Check internet access and your Python environment.
    pause
    exit /b 1
)
echo.
echo TkinterDnD2 installed. Restart CorridorKey.
pause
