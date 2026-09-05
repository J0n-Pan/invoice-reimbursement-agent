@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 正在启动票据流 Agent...
if defined LOCALAPPDATA (
    set "INVOICE_AGENT_CACHE_HOME=%LOCALAPPDATA%\InvoiceReimbursementAgent-v4"
) else (
    set "INVOICE_AGENT_CACHE_HOME=%~dp0.invoice-agent-cache-v4"
)
start "票据流 Agent" http://127.0.0.1:8765
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py
) else (
    echo 未找到项目虚拟环境，使用系统 Python 启动。
    python app.py
)
pause
