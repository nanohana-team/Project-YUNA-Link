@echo off
REM ============================================
REM Project YUNA Link - PROTOTYPE
REM ============================================

cd /d %~dp0

echo ============================================
echo  Starting YUNA Link (INPUT MODE)
echo ============================================
echo.

python apps/stt_llm_tts.py

echo.
echo ============================================
echo  Process finished
echo ============================================
pause