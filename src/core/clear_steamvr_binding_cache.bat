@echo off
:: Project YUNA Link - SteamVR binding cache cleaner
:: Run this AFTER re-installing the driver when thumbstick/grip don't work.
:: This forces SteamVR to re-read the input profile from scratch.

echo [YUNA] Stopping SteamVR...
taskkill /f /im vrserver.exe    >nul 2>&1
taskkill /f /im vrcompositor.exe>nul 2>&1
taskkill /f /im vrmonitor.exe   >nul 2>&1
taskkill /f /im vrdashboard.exe >nul 2>&1
timeout /t 2 >nul

set STEAM_CFG=%LOCALAPPDATA%\openvr
set STEAM_CFG2=%APPDATA%\openvr

:: Delete cached input bindings for yuna controller
echo [YUNA] Clearing input binding cache...
set INPUT_CACHE=%PROGRAMFILES(X86)%\Steam\config\input
if exist "%INPUT_CACHE%\yuna_*"      del /q "%INPUT_CACHE%\yuna_*"      >nul 2>&1
if exist "%INPUT_CACHE%\oculus_*"    del /q "%INPUT_CACHE%\oculus_*"    >nul 2>&1

:: Delete action manifest cache
set ACTION_CACHE=%PROGRAMFILES(X86)%\Steam\config
if exist "%ACTION_CACHE%\input\actions.json" del /q "%ACTION_CACHE%\input\actions.json" >nul 2>&1

echo [YUNA] Done. Please restart SteamVR.
echo.
echo If thumbstick still does not work after restart:
echo   1. Open SteamVR Settings - Controllers - Manage Controller Bindings
echo   2. Select "YUNA Controller" or "Oculus Touch"
echo   3. Verify thumbstick is bound to Locomotion/Turn
pause
