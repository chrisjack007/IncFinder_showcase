@echo off
setlocal DisableDelayedExpansion
set "PYTHONHOME="
set "PYTHONPATH="
set "PATH=%~dp0runtime;%~dp0runtime\Library\bin;%~dp0runtime\Scripts;%PATH%"
"%~dp0runtime\python.exe" -I -B -X utf8 "%~dp0portable\bootstrap.py" %*
set "rc=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %rc%
