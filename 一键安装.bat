@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   采集台 一键安装（小红书 + 抖音）
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [x] 没找到 Python。装 3.11 并勾选 "Add python.exe to PATH"
  echo     https://www.python.org/downloads/
  pause & exit /b 1
)
where node >nul 2>&1
if errorlevel 1 (
  echo [!] 没找到 Node.js：桌面壳需要它，纯网页版可以跳过
  echo     https://nodejs.org/
  echo.
  set "SKIP_DESKTOP=1"
)

echo [1/2] 建两个 venv 并装 websockets ...
call "scripts\setup.bat" || goto :err

if not defined SKIP_DESKTOP (
  echo [2/2] 装桌面壳依赖（electron，约 100MB）...
  pushd "desktop\xhs-desktop" && call npm install --no-audit --no-fund && popd
  pushd "desktop\dy-desktop" && call npm install --no-audit --no-fund && popd
) else (
  echo [2/2] 跳过桌面壳
)

echo.
echo 装好了。启动方式：
echo   网页版：scripts\xhs\启动.bat        ^(8788^)
echo            scripts\dy\app.py          ^(8789^)
echo   桌面壳：cd desktop\xhs-desktop ^&^& npx electron .
echo.
echo 第一次要在采集专用浏览器里扫码登录一次，之后免扫。
pause
exit /b 0

:err
echo.
echo 安装失败，检查 Python 3.11 是否在 PATH 上。
pause
exit /b 1
