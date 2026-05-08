# ATLAS Dusk Check Runner
# Invoked by Windows Task Scheduler each evening at 8:30 PM

$logDir    = "C:\Users\nasan\.claude\atlas\logs"
$agentScript = "C:\Users\nasan\.claude\atlas\atlas_agent.py"
$date      = Get-Date -Format "yyyy-MM-dd"
$logFile   = "$logDir\dusk_$date.log"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

@"
========================================
ATLAS DUSK CHECK — $date
Started: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
========================================
"@ | Out-File -FilePath $logFile -Encoding utf8

try {
    python $agentScript --phase dusk 2>&1 | Tee-Object -FilePath $logFile -Append
} catch {
    "ERROR launching atlas_agent.py: $_" | Out-File -FilePath $logFile -Append
}

@"

========================================
DUSK PHASE COMPLETE: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
========================================
"@ | Out-File -FilePath $logFile -Append
