@echo off
REM ============================================================
REM Project YUNA Link - scripts\install_driver.bat
REM Run as Administrator
REM ============================================================
setlocal ENABLEDELAYEDEXPANSION

echo ============================================================
echo  Project YUNA Link - Driver Installer
echo ============================================================
echo.

REM --- Source DLL path ---
set "REPO_ROOT=%~dp0.."
set "DLL_SRC=%REPO_ROOT%\src\driver_yuna\bin\win64"

echo [CHECK] DLL source: %DLL_SRC%

if not exist "%DLL_SRC%\driver_yuna.dll" (
    echo.
    echo [ERROR] driver_yuna.dll not found at:
    echo         %DLL_SRC%\driver_yuna.dll
    echo.
    echo  Please build with Visual Studio: Release ^| x64
    echo.
    pause
    exit /b 1
)
echo [OK] driver_yuna.dll found.

REM --- Locate SteamVR drivers folder ---
set "STEAMVR_DRIVERS="

echo.
echo [INFO] Searching for Steam installation...

for /f "tokens=2*" %%A in (
    'reg query "HKLM\SOFTWARE\WOW6432Node\Valve\Steam" /v InstallPath 2^>nul'
) do set "STEAM_PATH=%%B"

if "!STEAM_PATH!"=="" (
    for /f "tokens=2*" %%A in (
        'reg query "HKCU\SOFTWARE\Valve\Steam" /v SteamPath 2^>nul'
    ) do set "STEAM_PATH=%%B"
)

if not "!STEAM_PATH!"=="" (
    set "STEAMVR_DRIVERS=!STEAM_PATH!\steamapps\common\SteamVR\drivers"
    echo [OK] Steam found: !STEAM_PATH!
    echo [OK] SteamVR drivers: !STEAMVR_DRIVERS!
) else (
    echo [WARN] Steam not found in registry.
    echo.
    set /p STEAMVR_DRIVERS="Enter full path to SteamVR\drivers folder: "
)

if "!STEAMVR_DRIVERS!"=="" (
    echo [ERROR] SteamVR drivers path is empty.
    pause
    exit /b 1
)

if not exist "!STEAMVR_DRIVERS!" (
    echo [ERROR] SteamVR drivers folder does not exist:
    echo         !STEAMVR_DRIVERS!
    pause
    exit /b 1
)

REM --- Install ---
set "INSTALL_DIR=!STEAMVR_DRIVERS!\yuna"
echo.
echo [INFO] Installing to: !INSTALL_DIR!

if not exist "!INSTALL_DIR!\bin\win64" (
    mkdir "!INSTALL_DIR!\bin\win64"
    echo [OK] Created: !INSTALL_DIR!\bin\win64
)

echo.
echo [COPY] driver_yuna.dll ...
copy /Y "%DLL_SRC%\driver_yuna.dll" "!INSTALL_DIR!\bin\win64\" 
if errorlevel 1 ( echo [ERROR] Failed to copy driver_yuna.dll & pause & exit /b 1 )

echo [COPY] openvr_api.dll ...
if exist "%DLL_SRC%\openvr_api.dll" (
    copy /Y "%DLL_SRC%\openvr_api.dll" "!INSTALL_DIR!\bin\win64\"
    if errorlevel 1 ( echo [ERROR] Failed to copy openvr_api.dll & pause & exit /b 1 )
) else (
    echo [WARN] openvr_api.dll not found in DLL_SRC, skipping.
    echo        If SteamVR fails to load the driver, copy openvr_api.dll manually.
)

echo [COPY] driver.vrdrivermanifest ...
copy /Y "%REPO_ROOT%\src\driver_yuna\driver.vrdrivermanifest" "!INSTALL_DIR!\"
if errorlevel 1 ( echo [ERROR] Failed to copy driver.vrdrivermanifest & pause & exit /b 1 )

echo [COPY] resources\ ...
xcopy /E /I /Y "%REPO_ROOT%\src\driver_yuna\resources" "!INSTALL_DIR!\resources" >nul
if errorlevel 1 ( echo [ERROR] Failed to copy resources & pause & exit /b 1 )

echo.
echo ============================================================
echo  Install complete!
echo.
echo  Location: !INSTALL_DIR!
echo.
echo  Installed files:
dir /b "!INSTALL_DIR!\bin\win64\"
echo.
echo  Next steps:
echo    1. Restart SteamVR
echo    2. Run: python apps\pose_sender.py --mode test
echo ============================================================
echo.
pause
