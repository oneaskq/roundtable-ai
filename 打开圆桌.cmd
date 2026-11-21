@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if exist ".venv314\Scripts\python.exe" goto ready
if exist ".runtime\python-3.14.10\python.exe" (
  ".runtime\python-3.14.10\python.exe" -m venv .venv314
) else (
  where py >nul 2>nul
  if errorlevel 1 goto runtime_missing
  py -3.14 -c "import sys, ssl; v=tuple(map(int, ssl.OPENSSL_VERSION.split()[1].split('.'))); sys.exit(not (sys.version_info[:2] == (3, 14) and sys.version_info[:3] >= (3, 14, 10) and (3, 5, 9) <= v < (3, 6)))" >nul 2>nul
  if errorlevel 1 goto runtime_missing
  py -3.14 -m venv .venv314
)
if errorlevel 1 goto failed
:ready
".venv314\Scripts\python.exe" -c "import sys, ssl; v=tuple(map(int, ssl.OPENSSL_VERSION.split()[1].split('.'))); sys.exit(not (sys.version_info[:2] == (3, 14) and sys.version_info[:3] >= (3, 14, 10) and (3, 5, 9) <= v < (3, 6)))" >nul 2>nul
if errorlevel 1 goto runtime_missing
".venv314\Scripts\python.exe" -c "import django, cryptography, pyotp" >nul 2>nul
if not errorlevel 1 goto launch
".venv314\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto failed
".venv314\Scripts\python.exe" -m pip install -r requirements.lock
if errorlevel 1 goto failed
:launch
".venv314\Scripts\python.exe" launch.py
if errorlevel 1 goto failed
exit /b 0
:runtime_missing
echo 圆桌需要安全更新后的 Python 3.14 运行环境，请联系项目维护人员处理。
pause
exit /b 1
:failed
echo 启动未完成，请保留本窗口中的错误信息。
pause
exit /b 1
