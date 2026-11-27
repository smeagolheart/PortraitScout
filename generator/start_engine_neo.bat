@echo off
rem Starts Forge Neo (the "engine-neo" folder next to "generator") with its API on, for Portrait Scout.
rem Usage: start_engine_neo.bat PORT      Portrait Scout runs this by itself; you do not need to.
rem It reads the models, LoRAs, VAE and text encoders of the old engine too (--forge-ref-a1111-home).
rem Extra Forge options can be put in the PS_ENGINE_ARGS environment variable.
setlocal
set "NEO=%~dp0..\engine-neo"
set "OLD=%~dp0..\engine\webui"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=7861"
if not exist "%NEO%\webui.bat" (
  echo Forge Neo was not found: "%NEO%\webui.bat" is missing.
  exit /b 1
)
cd /d "%NEO%"
rem Forge opens its page in the browser when it starts; this tells it not to (Portrait Scout only uses the API).
set "SD_WEBUI_RESTARTING=1"
set "COMMANDLINE_ARGS=--uv --forge-ref-a1111-home "%OLD%" --api --api-server-stop --port %PORT% %PS_ENGINE_ARGS%"
call webui.bat
