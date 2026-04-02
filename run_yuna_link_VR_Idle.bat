@echo off
REM ============================================
REM Project YUNA Link - Input Mode Launcher
REM ============================================

cd /d %~dp0

echo ============================================
echo  Starting YUNA Link (INPUT MODE)
echo ============================================
echo.

python src/vr/yuna_link.py --mode idle

echo.
echo ============================================
echo  Process finished
echo ============================================
pause