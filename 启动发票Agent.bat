@echo off
setlocal
cd /d "%~dp0"

set "AGENT_URL=http://127.0.0.1:8765/"
set "AGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
set "INVOICE_AGENT_CACHE_HOME=%LOCALAPPDATA%\InvoiceReimbursementAgent-v3"

if not exist "%AGENT_PYTHON%" (
    echo Project virtual environment was not found: %AGENT_PYTHON%
    echo Please check the .venv folder before starting the Agent.
    pause
    exit /b 1
)

echo Checking Agent service...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $url='http://127.0.0.1:8765/'; $listener=netstat -ano -p tcp | Where-Object { $_ -match '127\.0\.0\.1:8765\s+0\.0\.0\.0:0\s+LISTENING' } | Select-Object -First 1; if (-not $listener) { $python=Join-Path (Get-Location) '.venv\Scripts\python.exe'; $env:INVOICE_AGENT_CACHE_HOME='%INVOICE_AGENT_CACHE_HOME%'; Start-Process -FilePath $python -ArgumentList 'app.py' -WorkingDirectory (Get-Location) -WindowStyle Minimized }; $ready=$false; for ($i=0; $i -lt 60; $i++) { try { $response=Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 1; if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { $ready=$true; break } } catch {}; Start-Sleep -Milliseconds 500 }; if (-not $ready) { throw 'Agent service did not become ready within 30 seconds' }; try { Start-Process $url } catch { Write-Host ('Browser launch was blocked. Open manually: ' + $url) }"
if errorlevel 1 (
    echo Agent start failed. Please check the project folder or port 8765.
    pause
    exit /b 1
)

echo Agent started. Opening the interface...
endlocal
