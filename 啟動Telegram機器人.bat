@echo off
chcp 65001 >nul
title Wheel 選擇權策略 Telegram 互動機器人

echo ======================================================
echo   Wheel 選擇權策略 Telegram 雙向互動機器人
echo ======================================================
echo.
echo 正在檢查 Python 環境並啟動機器人...
echo.

cd /d "%~dp0"

python tg_bot.py

if %errorlevel% neq 0 (
    echo.
    echo ❌ 機器人異常終止，結束代碼: %errorlevel%
    echo 請確認網路連線或金鑰設定是否正確。
    pause
)
