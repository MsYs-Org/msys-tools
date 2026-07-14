@echo off
rem Policy-independent Windows entry point for the versioned MSYS tools.
rem %* deliberately preserves the caller's original argument tail; PowerShell
rem receives it after -File and returns the underlying command exit code.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0msys.ps1" %*
set "MSYS_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %MSYS_EXIT_CODE%
