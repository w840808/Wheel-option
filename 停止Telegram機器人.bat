@echo off
chcp 65001 >nul
title 停止 Wheel 選擇權 Telegram 機器人

echo ======================================================
echo   正在尋找並終止正在運行的 tg_bot.py 背景進程...
echo ======================================================
echo.

powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tg_bot.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('已終止進程 PID: ' + $_.ProcessId) }"

echo.
echo ✅ 已停止所有 Telegram 機器人進程。
echo.
pause
