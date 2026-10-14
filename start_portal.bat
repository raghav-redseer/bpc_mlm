@echo off
setlocal
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312\;%LOCALAPPDATA%\Programs\Python\Python312\Scripts\;%PATH%"
cd /d "%~dp0"

powershell -NoProfile -Command "$c = New-Object System.Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1',8000); $c.Close(); exit 0 } catch { exit 1 }"

if %ERRORLEVEL%==0 (
    echo Loreal Automation Portal is already running.
    powershell -NoProfile -Command "Start-Process 'http://127.0.0.1:8000'"
    goto :eof
)

echo Starting Loreal Automation Portal...
start "Loreal Automation Portal" /min cmd /c "python app.py > portal_log.txt 2>&1"

REM Poll every half-second (up to ~30s) instead of a fixed wait, so the browser only opens
REM once the server is actually answering - a fixed short wait was opening the browser before
REM uvicorn had finished starting (imports pandas/openpyxl/the tagging engine on cold start),
REM which looked like "the link doesn't work" even though the server was fine seconds later.
set /a tries=0
:waitloop
powershell -NoProfile -Command "$c = New-Object System.Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1',8000); $c.Close(); exit 0 } catch { exit 1 }"
if %ERRORLEVEL%==0 goto :ready
set /a tries+=1
if %tries% GEQ 60 goto :timeout
timeout /t 1 /nobreak >nul
goto :waitloop

:ready
powershell -NoProfile -Command "Start-Process 'http://127.0.0.1:8000'"
goto :eof

:timeout
echo.
echo The portal did not start within 60 seconds. Check portal_log.txt in this folder for the error.
pause
