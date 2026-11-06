@echo off
chcp 65001 >nul
REM ============================================================
REM  Start dev server with the project virtualenv (.venv)
REM  Usage: double-click this file, or run it from PyCharm Terminal
REM ============================================================
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Create it first:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Project venv : %CD%\.venv\Scripts\python.exe
."venv\Scripts\python.exe" -c "import django;print('Django', django.get_version())"
."venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000
pause
