@echo off
REM ============================================================
REM Project YUNA Link - YOLO Person Detection Runner
REM ============================================================

setlocal

REM --- Move to repo root ---
cd /d %~dp0..

echo ============================================
echo  YOLO26 Person Detection
echo ============================================
echo.

REM --- Settings ---
set WINDOW_TITLE=YUNA Link - VR View
set MODEL=s
set IMGSZ=640
set CONF=0.30
set DEVICE=0

REM --- Pose ON/OFF ---
set USE_POSE=1

echo [INFO] Window Title : %WINDOW_TITLE%
echo [INFO] Model        : yolo26%MODEL%
echo [INFO] ImgSz        : %IMGSZ%
echo [INFO] Conf         : %CONF%
echo [INFO] Device       : %DEVICE%
echo [INFO] Pose         : %USE_POSE%
echo.

REM --- Run ---
if "%USE_POSE%"=="1" (
    python src/vision/yolo_person_detect.py ^
        --window-title "%WINDOW_TITLE%" ^
        --model %MODEL% ^
        --imgsz %IMGSZ% ^
        --conf %CONF% ^
        --device %DEVICE% ^
        --pose
) else (
    python src/vision/yolo_person_detect.py ^
        --window-title "%WINDOW_TITLE%" ^
        --model %MODEL% ^
        --imgsz %IMGSZ% ^
        --conf %CONF% ^
        --device %DEVICE%
)

echo.
echo [INFO] Finished.
pause