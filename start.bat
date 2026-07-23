@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" grok_register_ttk.py
) else (
  python grok_register_ttk.py
)
pause
