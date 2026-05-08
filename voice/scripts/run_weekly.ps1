# ATLAS Weekly Reflection Runner
# Invoked by Windows Task Scheduler each Sunday at 9:00 AM

$logDir      = "C:\Users\nasan\.claude\atlas\logs"
$agentScript = "C:\Users\nasan\.claude\atlas\atlas_agent.py"
$desktopDir  = "$env:USERPROFILE\Desktop\ATLAS Observatory"
$date        = Get-Date -Format "yyyy-MM-dd"
$weekNum     = Get-Date -UFormat "%Y-W%V"
$logFile     = "$logDir\weekly_$date.log"
$reportFile  = "$desktopDir\Weekly Reports\ATLAS_Weekly_Report_$weekNum.txt"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

@"
========================================
ATLAS WEEKLY REFLECTION — $weekNum
Started: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
========================================
"@ | Out-File -FilePath $logFile -Encoding utf8

try {
    python $agentScript --phase weekly 2>&1 | Tee-Object -FilePath $logFile -Append
} catch {
    "ERROR launching atlas_agent.py: $_" | Out-File -FilePath $logFile -Append
}

@"

========================================
WEEKLY PHASE COMPLETE: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
========================================
"@ | Out-File -FilePath $logFile -Append

# Balloon notification when weekly report is ready
if (Test-Path $reportFile) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.BalloonTipTitle = "ATLAS Weekly Report - $weekNum"
        $n.BalloonTipText = "Weekly reflection is ready in ATLAS Observatory on the desktop."
        $n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
        $n.Visible = $true
        $n.ShowBalloonTip(8000)
        Start-Sleep -Seconds 4
        $n.Dispose()
    } catch {
        "NOTE: Notification failed (non-critical): $_" | Out-File -FilePath $logFile -Append
    }
}
