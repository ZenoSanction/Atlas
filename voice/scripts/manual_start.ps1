# ATLAS Manual Start
# Double-click to run a phase manually outside of the scheduled tasks

$agentScript = "C:\Users\nasan\.claude\atlas\atlas_agent.py"
$logDir      = "C:\Users\nasan\.claude\atlas\logs"

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  ATLAS - Manual Start" -ForegroundColor Cyan
Write-Host "========================================`n" -ForegroundColor Cyan
Write-Host "  1. Dusk Check  (assess conditions, run session)" -ForegroundColor White
Write-Host "  2. Dawn Wrap-Up  (close session, write reports)" -ForegroundColor White
Write-Host "  3. Weekly Reflection" -ForegroundColor White
Write-Host "  4. Voice Interface" -ForegroundColor White
Write-Host ""

$choice = Read-Host "Select phase (1-4)"

$date    = Get-Date -Format "yyyy-MM-dd"
$logFile = "$logDir\manual_$date`_$(Get-Date -Format 'HHmmss').log"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

switch ($choice) {
    "1" {
        Write-Host "`nStarting Dusk Check..." -ForegroundColor Yellow
        python $agentScript --phase dusk 2>&1 | Tee-Object -FilePath $logFile
    }
    "2" {
        Write-Host "`nStarting Dawn Wrap-Up..." -ForegroundColor Yellow
        python $agentScript --phase dawn 2>&1 | Tee-Object -FilePath $logFile
    }
    "3" {
        Write-Host "`nStarting Weekly Reflection..." -ForegroundColor Yellow
        python $agentScript --phase weekly 2>&1 | Tee-Object -FilePath $logFile
    }
    "4" {
        Write-Host "`nStarting Voice Interface..." -ForegroundColor Yellow
        python "C:\Users\nasan\.claude\atlas\atlas_voice.py"
    }
    default {
        Write-Host "Invalid selection." -ForegroundColor Red
    }
}

Write-Host "`nPress any key to close..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
