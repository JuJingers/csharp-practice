@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo.
echo   C# trainer  ==^>  http://127.0.0.1:8777
echo   Close this window to stop the server.
echo.
start "" "http://127.0.0.1:8777"
python server.py
echo.
echo Server stopped.
pause
