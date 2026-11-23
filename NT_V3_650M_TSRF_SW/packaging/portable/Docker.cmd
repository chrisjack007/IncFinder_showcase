@echo off
setlocal DisableDelayedExpansion
"%~dp0image-launch.exe" %*
set "rc=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %rc%
