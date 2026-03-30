@echo off
REM ============================================================
REM Project YUNA Link - Start All Systems
REM ============================================================

cd /d %~dp0

echo ============================================
echo  YUNA Link System Boot
echo ============================================
echo.

REM --- VR Input ---
start "VR Input" cmd /k python src/vr/yuna_link.py --mode input

REM --- LLM + TTS ---
start "LLM TTS" cmd /k python apps/chat_llm_tts.py

REM --- Vision ---
start "Vision" cmd /k python src/vision/detect_player_dist.py --window-title "YUNA Link - VR View" --model x --imgsz 980

echo.
echo [INFO] All systems launched.
echo.
pause