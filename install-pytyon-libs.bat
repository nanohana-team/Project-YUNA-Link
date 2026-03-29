@echo off
echo ==============================
echo YUNA Local LLM Setup
echo ==============================

REM --- Pythonチェック ---
python --version >nul 2>&1
if errorlevel 1 (
echo [ERROR] Pythonが見つかりません
pause
exit /b
)

REM --- venv作成 ---
if not exist venv (
echo [INFO] 仮想環境を作成します...
python -m venv venv
)

REM --- venv有効化 ---
call venv\Scripts\activate

REM --- pip更新 ---
echo [INFO] pipを更新中...
python -m pip install --upgrade pip

REM --- torch (CUDA版) ---
echo [INFO] PyTorchをインストール中...
pip install torch --index-url https://download.pytorch.org/whl/cu121

REM --- requirements ---
echo [INFO] 依存関係をインストール中...
pip install -r requirements.txt

echo ==============================
echo Setup Complete!
echo ==============================
pause
