# ATLAS Setup Script
# Run once to install Python dependencies and pull the Ollama model
# Run as: powershell -ExecutionPolicy Bypass -File setup.ps1

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ATLAS Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Python dependencies
Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
pip install requests faster-whisper sounddevice numpy pyttsx3
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed. Make sure Python and pip are in PATH." -ForegroundColor Red
    exit 1
}
Write-Host "Python dependencies installed." -ForegroundColor Green
Write-Host ""

# Ollama model
Write-Host "Pulling Ollama model (qwen2.5:7b)..." -ForegroundColor Yellow
Write-Host "This is ATLAS's permanent local brain (~4.7 GB)." -ForegroundColor Gray
Write-Host "Make sure Ollama is running before proceeding." -ForegroundColor Gray
Write-Host ""

ollama pull qwen2.5:7b
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Ollama pull failed." -ForegroundColor Red
    Write-Host "Start Ollama and re-run this script." -ForegroundColor Red
    exit 1
}
Write-Host "Model ready." -ForegroundColor Green
Write-Host ""

# Verify agent
Write-Host "Verifying ATLAS agent..." -ForegroundColor Yellow
python "C:\Users\nasan\.claude\atlas\atlas_agent.py" --help
if ($LASTEXITCODE -eq 0) {
    Write-Host "ATLAS agent verified." -ForegroundColor Green
} else {
    Write-Host "WARNING: atlas_agent.py check failed - review output above." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ATLAS is ready." -ForegroundColor Cyan
Write-Host " Scheduled tasks:" -ForegroundColor Cyan
Write-Host "   Dusk Check    - nightly  8:30 PM" -ForegroundColor White
Write-Host "   Dawn Wrap-Up  - nightly  5:00 AM" -ForegroundColor White
Write-Host "   Weekly Report - Sundays  9:00 AM" -ForegroundColor White
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
