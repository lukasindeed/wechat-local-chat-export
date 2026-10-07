@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
    python -X utf8 "%~dp0wechat_export.py" %*
) else (
    py -3 -X utf8 "%~dp0wechat_export.py" %*
)
pause
