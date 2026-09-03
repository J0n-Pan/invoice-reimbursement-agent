@echo off
setlocal

echo Stopping Agent service...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$owners=@(netstat -ano -p tcp | Where-Object { $_ -match '127\.0\.0\.1:8765\s+0\.0\.0\.0:0\s+LISTENING' } | ForEach-Object { [regex]::Match($_.ToString(), '\s+(\d+)\s*$').Groups[1].Value } | Where-Object { $_ } | Select-Object -Unique); if (-not $owners) { Write-Host 'No Agent service is listening on port 8765.'; exit 0 }; foreach ($ownerId in $owners) { if (Get-Process -Id $ownerId -ErrorAction SilentlyContinue) { Stop-Process -Id $ownerId -Force; Write-Host ('Stopped process ' + $ownerId) } }; Start-Sleep -Milliseconds 500; Write-Host 'Agent service stopped.'"
if errorlevel 1 (
    echo Agent stop failed. Please try again as administrator.
    pause
    exit /b 1
)

echo Done.
endlocal
