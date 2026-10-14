@echo off
REM Install the cloud memory sync tunnel so it comes up automatically at logon.
REM
REM Puts two files in the Startup folder:
REM   cloud_memory_tunnel.cmd         - the reconnect loop
REM   cloud_memory_tunnel_hidden.vbs  - launches the loop with no console window
REM
REM Why the Startup folder and not Task Scheduler: registering a scheduled task
REM needs the script path as a command-line argument, and this project lives under
REM a user profile with non-ASCII characters - cmd.exe and schtasks disagree about
REM the codepage and the task ends up with a mangled path. A file dropped in
REM Startup has no such problem.
REM
REM Keep this file ASCII-only (cmd.exe reads .cmd in the OEM codepage).
setlocal
set "SRC=%~dp0"
set "DST=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

if not exist "%SRC%cloud_memory_tunnel.cmd" (
    echo Missing cloud_memory_tunnel.cmd next to this script.
    exit /b 3
)
if not exist "%SRC%cloud_memory_tunnel_hidden.vbs" (
    echo Missing cloud_memory_tunnel_hidden.vbs next to this script.
    exit /b 3
)
if not exist "%DST%" (
    echo Startup folder not found: "%DST%"
    exit /b 3
)

copy /y "%SRC%cloud_memory_tunnel.cmd" "%DST%\" >nul || exit /b 4
copy /y "%SRC%cloud_memory_tunnel_hidden.vbs" "%DST%\" >nul || exit /b 4

echo Installed. The tunnel will start hidden at every logon.
echo   folder: "%DST%"
echo   files : cloud_memory_tunnel.cmd / cloud_memory_tunnel_hidden.vbs
echo.
echo Remove it later with: scripts\uninstall_cloud_tunnel_autostart.cmd
exit /b 0
