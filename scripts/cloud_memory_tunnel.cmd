@echo off
REM Cloud memory sync tunnel: forwards local 8790 to the server's 127.0.0.1:8790.
REM The server does NOT expose 8790 to the internet, so this tunnel is the only way in.
REM
REM Runs in a reconnect loop: if the connection drops (network change, sleep/resume,
REM server restart), it retries every 10 seconds instead of just dying.
REM Keep this window open when running it by hand; for autostart use the scheduled
REM task registered by scripts/install_cloud_tunnel_autostart.cmd (runs hidden).
REM
REM NOTE: keep this file ASCII-only. cmd.exe reads .cmd in the OEM codepage and
REM Chinese comments turn into mojibake (harmless but confusing in error output).
setlocal
REM Fill these in for your own server. Do NOT commit your real host or key name.
set "KEY=%USERPROFILE%\.ssh\your_key_name"
set "HOST=root@your-server-address"
set "LOCAL_PORT=8790"
set "REMOTE_PORT=8790"

title Cloud memory sync tunnel

:loop
echo [%date% %time%] connecting %LOCAL_PORT% -^> %REMOTE_PORT% ...
ssh -i "%KEY%" -N ^
    -o BatchMode=yes ^
    -o ConnectTimeout=15 ^
    -o ServerAliveInterval=30 ^
    -o ServerAliveCountMax=3 ^
    -o ExitOnForwardFailure=yes ^
    -L %LOCAL_PORT%:127.0.0.1:%REMOTE_PORT% ^
    %HOST%
echo [%date% %time%] tunnel closed, retrying in 10s
REM Sleep via ping, NOT `timeout /t`: when this script runs hidden (no visible
REM console) timeout.exe can refuse with "input redirection is not supported",
REM and the loop would then spin with no delay at all.
ping -n 11 127.0.0.1 >nul 2>&1
goto loop
