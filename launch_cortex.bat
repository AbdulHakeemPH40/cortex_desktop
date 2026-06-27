@echo off
REM Cortex AI Agent - Direct Launch

cd /d "%~dp0"

echo ==================================================
echo   Cortex AI Agent
echo ==================================================
echo.
echo Starting Cortex IDE...
echo.

python src\main.py

echo.
echo Cortex has exited.
pause
