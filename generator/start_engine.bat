@echo off
rem Starts Portrait Scout's own copy of Forge (the "engine" folder next to "generator") with its API on.
rem Usage: start_engine.bat PORT      Portrait Scout runs this by itself; you do not need to.
rem Extra Forge options can be put in the PS_ENGINE_ARGS environment variable.
setlocal
set "ENGINE=%~dp0..\engine"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=7861"
if not exist "%ENGINE%\webui\webui.bat" (
  echo The engine was not found: "%ENGINE%\webui\webui.bat" is missing. See generator\README.md.
  exit /b 1
)
cd /d "%ENGINE%"
if exist environment.bat call environment.bat
cd /d "%ENGINE%\webui"
rem Forge opens its page in the browser when it starts; this tells it not to (Portrait Scout only uses the API).
set "SD_WEBUI_RESTARTING=1"
set "COMMANDLINE_ARGS=--api --api-server-stop --port %PORT% %PS_ENGINE_ARGS%"
call webui.bat
