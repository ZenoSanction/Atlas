# ATLAS Dawn Wrap-Up Runner
# Invoked by Windows Task Scheduler each morning at 5:00 AM

$logDir      = "C:\Users\nasan\.claude\atlas\logs"
$agentScript = "C:\Users\nasan\.claude\atlas\atlas_agent.py"
$desktopDir  = "$env:USERPROFILE\Desktop\ATLAS Observatory"
$date        = Get-Date -Format "yyyy-MM-dd"
$logFile     = "$logDir\dawn_$date.log"
$reportFile  = "$desktopDir\Morning Reports\ATLAS_Morning_Report_$date.txt"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

@"
========================================
ATLAS DAWN WRAP-UP — $date
Started: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
========================================
"@ | Out-File -FilePath $logFile -Encoding utf8

try {
    python $agentScript --phase dawn 2>&1 | Tee-Object -FilePath $logFile -Append
} catch {
    "ERROR launching atlas_agent.py: $_" | Out-File -FilePath $logFile -Append
}

@"

========================================
DAWN PHASE COMPLETE: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
========================================
"@ | Out-File -FilePath $logFile -Append

# Balloon notification when morning report is ready
if (Test-Path $reportFile) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.BalloonTipTitle = "ATLAS Morning Report - $date"
        $n.BalloonTipText = "Morning report is ready in ATLAS Observatory on the desktop."
        $n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
        $n.Visible = $true
        $n.ShowBalloonTip(8000)
        Start-Sleep -Seconds 4
        $n.Dispose()
    } catch {
        "NOTE: Notification failed (non-critical): $_" | Out-File -FilePath $logFile -Append
    }
}
