@echo off
rem Policy-independent Windows entry point for the versioned MSYS tools.
rem %* deliberately preserves the caller's original argument tail; PowerShell
rem receives it after -File and returns the underlying command exit code.
setlocal
set "MSYS_SCRIPT_ROOT=%~dp0"
if /I "%~1"=="--native" goto msys_native
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%MSYS_SCRIPT_ROOT%msys.ps1" %*
set "MSYS_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %MSYS_EXIT_CODE%

:msys_native
shift
set /a MSYS_NATIVE_ARG_COUNT=0
:msys_native_args
if "%~1"=="" goto msys_native_run
set "MSYS_NATIVE_ARG_%MSYS_NATIVE_ARG_COUNT%=%~1"
set /a MSYS_NATIVE_ARG_COUNT+=1
shift
goto msys_native_args
:msys_native_run
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%MSYS_SCRIPT_ROOT%msys-native.ps1"
set "MSYS_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %MSYS_EXIT_CODE%
