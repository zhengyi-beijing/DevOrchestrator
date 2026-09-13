@echo off
setlocal
for %%I in ("%~dp0..") do set "DEVORCH_REPO=%%~fI"
set "PYTHONPATH=%DEVORCH_REPO%\src"
python -m dev_orchestrator %*
exit /b %ERRORLEVEL%
