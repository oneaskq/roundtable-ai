@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv314\Scripts\python.exe" (
  echo 请先双击“打开圆桌.cmd”完成本机安装。
  pause
  exit /b 1
)
".venv314\Scripts\python.exe" -u manage.py verify_model --interactive
echo.
pause
