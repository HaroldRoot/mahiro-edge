@echo off
rem ASCII-only launcher. Do NOT add non-ASCII chars here: cmd.exe parses .cmd
rem files in the OEM codepage (GBK on zh-CN), so any UTF-8 Chinese would corrupt
rem the control flow. All Chinese UI text lives in the .ps1 (UTF-8 BOM) instead.
setlocal

rem --- self-elevate to administrator ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    exit /b
)

set "SCRIPT=%~dp0src\Install.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*

echo.
pause
