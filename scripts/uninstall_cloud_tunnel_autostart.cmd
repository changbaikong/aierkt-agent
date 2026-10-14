@echo off
REM Remove the cloud memory sync tunnel autostart (leaves the repo scripts alone).
REM Keep this file ASCII-only (cmd.exe reads .cmd in the OEM codepage).
setlocal
set "DST=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "FOUND=0"

if exist "%DST%\cloud_memory_tunnel.cmd" (
    del /f /q "%DST%\cloud_memory_tunnel.cmd"
    set "FOUND=1"
)
if exist "%DST%\cloud_memory_tunnel_hidden.vbs" (
    del /f /q "%DST%\cloud_memory_tunnel_hidden.vbs"
    set "FOUND=1"
)

if "%FOUND%"=="1" (
    echo Removed autostart files from "%DST%".
    echo A tunnel that is already running keeps running until you close it.
) else (
    echo Nothing to remove in "%DST%".
)
exit /b 0
