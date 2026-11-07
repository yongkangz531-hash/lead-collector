@echo off
chcp 936 >nul
title 线索采集台
cd /d "%~dp0"

echo ==========================================
echo    线索采集台  启动中...
echo ==========================================
echo.

rem 1) 检查采集用 Chrome（调试端口 9333）
netstat -ano | findstr ":9333" | findstr LISTENING >nul
if errorlevel 1 (
  echo [1/3] 启动采集浏览器...
  start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
    --remote-debugging-port=9333 ^
    --user-data-dir="%~dp0..\xhs-profile" ^
    --no-first-run --no-default-browser-check ^
    --proxy-server="direct://" --proxy-bypass-list="*" ^
    "https://www.xiaohongshu.com/explore"
  ping -n 6 127.0.0.1 >nul
) else (
  echo [1/3] 采集浏览器已在运行
)

rem 2) 检查本地服务 8788
netstat -ano | findstr ":8788" | findstr LISTENING >nul
if errorlevel 1 (
  echo [2/3] 启动本地服务...
  start "线索采集台-服务" /min "%~dp0.venv\Scripts\python.exe" app.py
  ping -n 4 127.0.0.1 >nul
) else (
  echo [2/3] 本地服务已在运行
)

echo [3/3] 打开界面...
start "" "http://127.0.0.1:8788"

echo.
echo 界面已打开。第一次用请确认那个小红书窗口是登录状态，没登录就扫码。
echo 采集时别关窗口，跑完会自己在表格里出结果。
echo.
pause
