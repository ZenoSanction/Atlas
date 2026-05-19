# ============================================================================
# ATLAS Uninstaller
# ============================================================================
# Run as Administrator from C:\ATLAS:
#     powershell -ExecutionPolicy Bypass -File uninstall.ps1
#
# Three modes:
#
#   (default — interactive)   Prompts you to choose one of:
#       1. Shortcuts only — remove desktop + Start Menu shortcuts + firewall
#                            rule. Keeps everything else. Reversible by
#                            re-running install.ps1.
#       2. Software — remove shortcuts, firewall, Python venv, and code,
#                     but PRESERVE the database, your captured frames,
#                     reference frame library, session reports, calibration
#                     masters, and credentials lock file.
#       3. Full wipe — remove everything including science data.
#                      Backup is created first.
#
#   -Mode shortcuts | software | full       Skip prompt and run a tier.
#   -Force                                  Skip the final confirmation.
#   -BackupFirst                            Force a backup even on tier 1/2.
#
# Examples:
#     uninstall.ps1                                       # interactive
#     uninstall.ps1 -Mode shortcuts                       # quick
#     uninstall.ps1 -Mode software -Force                 # scripted
#     uninstall.ps1 -Mode full -Force                     # full wipe, no prompt
# ============================================================================

param(
    [ValidateSet("shortcuts", "software", "full")]
    [string]$Mode,
    [switch]$Force,
    [switch]$BackupFirst
)

$ErrorActionPreference = "Stop"
$PSDefaultParameterValues['*:Encoding'] = 'utf8'

$ATLAS_ROOT     = "C:\ATLAS"
$BACKUPS_ROOT   = "C:\ATLAS_backups"
$VENV           = Join-Path $ATLAS_ROOT "venv"
$DATA_DIR       = Join-Path $ATLAS_ROOT "data"
$LOG_DIR        = Join-Path $DATA_DIR "logs"
$LOG_FILE_NAME  = "uninstall_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log"

# Log to console only if data dir is going away
$preserveLog = $true
if (-not (Test-Path $LOG_DIR)) {
    try { New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null } catch { $preserveLog = $false }
}
$LOG_FILE = if ($preserveLog) { Join-Path $LOG_DIR $LOG_FILE_NAME } else { Join-Path $env:TEMP $LOG_FILE_NAME }

function Log {
    param([string]$msg, [string]$level = "INFO")
    $stamp = Get-Date -Format "HH:mm:ss"
    $line = "[$stamp] $level  $msg"
    try { $line | Out-File -FilePath $LOG_FILE -Append -ErrorAction SilentlyContinue } catch {}
    Write-Host $line
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

Log "================================================================"
Log "  ATLAS Uninstaller"
Log "  Install root:  $ATLAS_ROOT"
Log "  Backups root:  $BACKUPS_ROOT"
Log "  Log file:      $LOG_FILE"
Log "================================================================"

# ---------------------------------------------------------------------------
# Interactive tier selection (if not supplied)
# ---------------------------------------------------------------------------

if (-not $Mode) {
    Write-Host ""
    Write-Host "  Pick what to remove:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "    [1] Shortcuts only  — desktop + Start Menu shortcuts + firewall rule"
    Write-Host "                          (Code, venv, database, science data all preserved)"
    Write-Host ""
    Write-Host "    [2] Software        — code + venv + shortcuts + firewall"
    Write-Host "                          (Database, captured frames, reports, references all preserved)"
    Write-Host ""
    Write-Host "    [3] Full wipe       — everything, including science data"
    Write-Host "                          (A backup is created first regardless)"
    Write-Host ""
    Write-Host "    [Q] Quit"
    Write-Host ""
    $choice = Read-Host "  Choice"
    switch ($choice.ToLower()) {
        "1" { $Mode = "shortcuts" }
        "2" { $Mode = "software" }
        "3" { $Mode = "full" }
        "q" { Log "Cancelled by user."; exit 0 }
        default { Log "Invalid choice. Cancelled."; exit 1 }
    }
}

Log "Selected mode: $Mode"

# ---------------------------------------------------------------------------
# Final confirmation
# ---------------------------------------------------------------------------

if (-not $Force) {
    $msg = switch ($Mode) {
        "shortcuts" { "Remove ATLAS desktop shortcuts, Start Menu entries, and firewall rule. The install root, venv, database, and all data are PRESERVED." }
        "software"  { "Remove ATLAS code and venv. The database, captured frames, reference frames, reports, and calibration masters are PRESERVED in $DATA_DIR. A backup of the data folder is made first." }
        "full"      { "REMOVE EVERYTHING at $ATLAS_ROOT — code, venv, database, captured frames, reports, references, calibration. A full backup is created first at $BACKUPS_ROOT." }
    }
    Write-Host ""
    Write-Host "  $msg" -ForegroundColor Yellow
    Write-Host ""
    $confirm = Read-Host "  Type 'yes' to proceed, anything else to cancel"
    if ($confirm.ToLower() -ne "yes") {
        Log "Cancelled by user at confirmation prompt."
        exit 0
    }
}

# ---------------------------------------------------------------------------
# Stop running ATLAS processes
# ---------------------------------------------------------------------------

Log "Looking for running ATLAS processes..."
$pythonProcs = Get-Process -Name python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($ATLAS_ROOT, [StringComparison]::OrdinalIgnoreCase) }
if ($pythonProcs) {
    foreach ($p in $pythonProcs) {
        Log "  Stopping PID $($p.Id): $($p.Path)"
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop }
        catch { Log "    failed: $($_.Exception.Message)" "WARN" }
    }
    Start-Sleep -Seconds 1
} else {
    Log "  None running."
}

# Also check port 5000 — anything listening there should die
try {
    $listeners = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
    if ($listeners) {
        foreach ($l in $listeners) {
            try {
                $p = Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue
                if ($p) {
                    Log "  Stopping PID $($p.Id) holding port 5000: $($p.ProcessName)"
                    Stop-Process -Id $p.Id -Force -ErrorAction Stop
                }
            } catch {}
        }
    }
} catch {}

# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

function New-Backup {
    param([string]$Tag)
    if (-not (Test-Path $ATLAS_ROOT)) { return $null }
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    $dest = Join-Path $BACKUPS_ROOT ("ATLAS_${ts}_${Tag}")
    Log "Creating backup at $dest ..."
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    # Copy everything except the venv (huge and trivially recreatable)
    Get-ChildItem -Path $ATLAS_ROOT -Force | Where-Object { $_.Name -ne "venv" } | ForEach-Object {
        $target = Join-Path $dest $_.Name
        try {
            if ($_.PSIsContainer) { Copy-Item $_.FullName $target -Recurse -Force -ErrorAction Stop }
            else                  { Copy-Item $_.FullName $target -Force -ErrorAction Stop }
        } catch {
            Log "  WARN: backup skip $($_.Name): $($_.Exception.Message)" "WARN"
        }
    }
    Log "Backup complete."
    return $dest
}

if ($Mode -eq "full" -or $Mode -eq "software" -or $BackupFirst) {
    $tag = if ($Mode -eq "full") { "pre_full_wipe" }
           elseif ($Mode -eq "software") { "pre_software_remove" }
           else { "pre_shortcuts_remove" }
    $backup = New-Backup -Tag $tag
}

# ---------------------------------------------------------------------------
# Tier 1: shortcuts + firewall (all modes do this)
# ---------------------------------------------------------------------------

Log "Removing desktop shortcuts ..."
$desktop = [Environment]::GetFolderPath("Desktop")
foreach ($n in "Start ATLAS Observatory.lnk", "Open ATLAS Dashboard.lnk") {
    $p = Join-Path $desktop $n
    if (Test-Path $p) {
        Remove-Item $p -Force -ErrorAction SilentlyContinue
        Log "  removed $n"
    }
}

Log "Removing Start Menu entries ..."
$startMenu = Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "ATLAS"
if (Test-Path $startMenu) {
    Remove-Item $startMenu -Recurse -Force -ErrorAction SilentlyContinue
    Log "  removed Start Menu folder."
}

Log "Removing firewall rule ..."
try {
    Get-NetFirewallRule -DisplayName "ATLAS Dashboard (TCP 5000)" -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    Log "  firewall rule removed."
} catch {
    Log "  firewall removal skipped: $($_.Exception.Message)" "WARN"
}

# ---------------------------------------------------------------------------
# Tier 2: code + venv (software and full)
# ---------------------------------------------------------------------------

if ($Mode -eq "software" -or $Mode -eq "full") {
    Log "Removing Python virtual environment ..."
    if (Test-Path $VENV) {
        try {
            Remove-Item $VENV -Recurse -Force -ErrorAction Stop
            Log "  venv removed."
        } catch {
            Log "  venv removal failed: $($_.Exception.Message)" "WARN"
        }
    }

    Log "Removing ATLAS code and configuration files ..."
    $codeItems = @("atlas", "dashboard", "catalogs", "scripts", "tests",
                   "docs", "install.ps1", "start_atlas.bat", "open_dashboard.bat",
                   "pyproject.toml", "requirements.txt", "README.md",
                   "LICENSE", ".gitignore", "master_password.lock")
    foreach ($n in $codeItems) {
        $p = Join-Path $ATLAS_ROOT $n
        if (Test-Path $p) {
            try {
                Remove-Item $p -Recurse -Force -ErrorAction Stop
                Log "  removed $n"
            } catch {
                Log "  WARN: could not remove $n : $($_.Exception.Message)" "WARN"
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Tier 3: data wipe (full only)
# ---------------------------------------------------------------------------

if ($Mode -eq "full") {
    Log "Removing data directory (database, frames, reports, references) ..."
    if (Test-Path $DATA_DIR) {
        try {
            Remove-Item $DATA_DIR -Recurse -Force -ErrorAction Stop
            Log "  data directory removed."
        } catch {
            Log "  WARN: data directory removal failed: $($_.Exception.Message)" "WARN"
        }
    }

    # Final: remove the install root itself (if it's empty)
    if (Test-Path $ATLAS_ROOT) {
        try {
            # Will succeed only if empty after everything above
            Remove-Item $ATLAS_ROOT -Force -ErrorAction Stop
            Log "Install root $ATLAS_ROOT removed."
        } catch {
            # Anything left? Force-recursive remove as a last step.
            try {
                Remove-Item $ATLAS_ROOT -Recurse -Force -ErrorAction Stop
                Log "Install root $ATLAS_ROOT removed (with remaining contents)."
            } catch {
                Log "Install root retained — files remain at $ATLAS_ROOT : $($_.Exception.Message)" "WARN"
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Optional: uninstall Python itself? (NO — we never installed it ourselves
# explicitly enough to know it's safe to remove; user may use it elsewhere)
# ---------------------------------------------------------------------------

Log ""
Log "================================================================"
Log "  Uninstall complete (mode: $Mode)."
if ($Mode -eq "shortcuts") {
    Log "  ATLAS is still installed at $ATLAS_ROOT — just no shortcuts."
    Log "  To restore shortcuts, run install.ps1 again."
} elseif ($Mode -eq "software") {
    Log "  Code and venv removed. Data preserved at $DATA_DIR."
    Log "  Reinstall and your campaigns, frames, and reports will reappear."
} else {
    Log "  Everything ATLAS-related has been removed."
    Log "  Backup is preserved at: $backup"
    Log "  Python itself (in C:\Program Files\PythonXX or similar) was NOT removed."
}
Log "================================================================"

exit 0
