@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 正在启动票据流 Agent...
start "票据流 Agent" http://127.0.0.1:8765
python app.py
pause

