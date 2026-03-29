@echo off
echo ==============================
echo YUNA Local LLM CLI
echo ==============================

REM --- カレントディレクトリをこのbatの場所に移動 ---
cd /d %~dp0

REM --- 仮想環境有効化 ---
call venv\Scripts\activate

REM --- Python実行 ---
python src\llm\local_llm.py --mode cli

echo.
echo ==============================
echo CLI 終了
echo ==============================
pause
