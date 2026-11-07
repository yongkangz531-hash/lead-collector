@echo off
setlocal
cd /d "%~dp0"
echo === lead-collector-pack setup ===
for %%D in (xhs dy) do (
  if not exist "%%D\.venv\Scripts\python.exe" (
    echo [%%D] creating venv...
    python -m venv "%%D\.venv" || goto :err
  ) else (
    echo [%%D] venv exists
  )
  echo [%%D] installing requirements...
  "%%D\.venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || goto :err
)
echo.
echo done. next: cd ..\desktop\xhs-desktop ^&^& npm install
pause
exit /b 0

:err
echo.
echo FAILED - check that python 3.11 is on PATH
pause
exit /b 1
