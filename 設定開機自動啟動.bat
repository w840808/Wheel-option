@echo off
chcp 65001 >nul
title 設定 Wheel 機器人開機自動啟動

echo ======================================================
echo   設定 Wheel 選擇權 Telegram 機器人【開機自動啟動】
echo ======================================================
echo.

cd /d "%~dp0"

echo 正在複製開機啟動腳本至 Windows Startup 資料夾...
copy /y "WheelTelegramBot.vbs" "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\" >nul

if %errorlevel% equ 0 (
    echo ✅ 設定成功！
    echo 系統已為您設置完畢，以後每次電腦開機時，機器人都會在背景「靜默無黑窗」常駐運行。
    echo.
    echo 正在為您立即啟動背景機器人...
    wscript "啟動Telegram機器人_靜默背景版.vbs"
    echo 🟢 機器人已在背景啟動！你可以拿起手機在 Telegram 點擊按鈕或輸入代碼測試了。
) else (
    echo ❌ 設定失敗，請嘗試手動將 WheelTelegramBot.vbs 複製到 Startup 資料夾。
)

echo.
pause
