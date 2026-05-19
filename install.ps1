# ============================================================================
# ATLAS Silent Installer
# ============================================================================
# Run as Administrator from C:\ATLAS:
#     powershell -ExecutionPolicy Bypass -File install.ps1
#
# This installer is silent and idempotent. It:
#   1. Verifies / installs Python 3.11+ (winget or download fallback)
#   2. Creates a private virtual environment at C:\ATLAS\venv
#   3. Installs all Python dependencies from requirements.txt
#   4. Initialises the SQLite database with full schema and seed data
#   5. Creates a Windows Firewall rule for inbound TCP port 5000
#   6. Creates two desktop shortcuts: Start ATLAS, Open Dashboard
#   7. Creates a Start Menu folder with the same shortcuts
#   8. Verifies optional third-party tools (NINA, PHD2, ASTAP, Siril)
#      and emits a clear notice for anything missing — does NOT block.
#
# Logs everything to C:\ATLAS\data\logs\install_<timestamp>.log.
# ============================================================================

$ErrorActionPreference = "Stop"
$PSDefaultParameterValues['*:Encoding'] = 'utf8'

$ATLAS_ROOT  = "C:\ATLAS"
$VENV        = Join-Path $ATLAS_ROOT "venv"
$PYTHON_VENV = Join-Path $VENV "Scripts\python.exe"
$REQUIREMENTS = Join-Path $ATLAS_ROOT "requirements.txt"
$DATA_DIR    = Join-Path $ATLAS_ROOT "data"
$LOG_DIR     = Join-Path $DATA_DIR "logs"
$LOG_FILE    = Join-Path $LOG_DIR ("install_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")

# Required Python version (major, minor)
$PY_MAJOR = 3
$PY_MINOR = 11

# Optional third-party tools we look for
$OPTIONAL_TOOLS = @(
    @{ Name = "NINA";         RegKey = "HKLM:\SOFTWARE\WOW6432Node\NINA";  Hint = "https://nighttime-imaging.eu/" },
    @{ Name = "PHD2";         Path   = "C:\Program Files (x86)\PHDGuiding2\phd2.exe"; Hint = "https://openphdguiding.org/" },
    @{ Name = "ASTAP";        Path   = "C:\Program Files\astap\astap.exe"; Hint = "https://www.hnsky.org/astap.htm" },
    @{ Name = "Siril";        Path   = "C:\Program Files\Siril\bin\siril.exe"; Hint = "https://siril.org/" },
    @{ Name = "AutoStakkert"; Path   = "C:\Program Files\AutoStakkert!4\AutoStakkert.exe"; Hint = "https://www.autostakkert.com/" }
)

# ---------------------------------------------------------------------------
function Ensure-Dir([string]$p) { if (!(Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null } }

Ensure-Dir $DATA_DIR
Ensure-Dir $LOG_DIR

function Log {
    param([string]$msg, [string]$level = "INFO")
    $stamp = Get-Date -Format "HH:mm:ss"
    $line = "[$stamp] $level  $msg"
    $line | Tee-Object -FilePath $LOG_FILE -Append | Write-Host
}

function Fail([string]$msg) { Log "FATAL: $msg" "ERROR"; throw $msg }

Log "================================================================"
Log "  ATLAS Installer"
Log "  Install root: $ATLAS_ROOT"
Log "  Log file:     $LOG_FILE"
Log "================================================================"

# ---------------------------------------------------------------------------
# 1. Python check + install
# ---------------------------------------------------------------------------

function Get-PythonPath {
    # Prefer 'py' launcher if available
    try {
        $candidates = (& py -0p 2>$null) -split "`r?`n" | Where-Object { $_ -match "\-V:?(\d+\.\d+)" }
        foreach ($c in $candidates) {
            if ($c -match "^\s*-V:?(\d+)\.(\d+)\s+\*?\s*(.*)$") {
                $major = [int]$matches[1]; $minor = [int]$matches[2]; $path = $matches[3].Trim()
                if (($major -eq $PY_MAJOR -and $minor -ge $PY_MINOR) -or $major -gt $PY_MAJOR) {
                    if (Test-Path $path) { return $path }
                }
            }
        }
    } catch {}
    # Fall back: any python.exe on PATH
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $v = & $cmd.Source -c "import sys; print(sys.version_info[:2])"
        if ($v -match "\((\d+),\s*(\d+)\)") {
            $major = [int]$matches[1]; $minor = [int]$matches[2]
            if (($major -eq $PY_MAJOR -and $minor -ge $PY_MINOR) -or $major -gt $PY_MAJOR) {
                return $cmd.Source
            }
        }
    }
    return $null
}

function Install-Python {
    Log "Installing Python $PY_MAJOR.$PY_MINOR..."
    # Try winget first
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        try {
            & winget install --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements
            Start-Sleep -Seconds 3
            $p = Get-PythonPath
            if ($p) { return $p }
        } catch {
            Log "winget install failed: $($_.Exception.Message). Falling back to direct download." "WARN"
        }
    }

    # Direct download
    $url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    $installer = Join-Path $env:TEMP "python-3.11.9-installer.exe"
    Log "Downloading Python from $url ..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    } catch {
        Fail "Could not download Python: $($_.Exception.Message)"
    }
    Log "Running Python installer (silent, all users) ..."
    $args = @("/quiet", "InstallAllUsers=1", "PrependPath=1",
              "Include_test=0", "Include_doc=0", "Include_launcher=1")
    $proc = Start-Process -FilePath $installer -ArgumentList $args -PassThru -Wait
    if ($proc.ExitCode -ne 0) { Fail "Python installer returned exit $($proc.ExitCode)" }
    Start-Sleep -Seconds 3
    $p = Get-PythonPath
    if ($null -eq $p) { Fail "Python installation completed but no valid interpreter was found." }
    return $p
}

$python = Get-PythonPath
if (-not $python) {
    Log "Python $PY_MAJOR.$PY_MINOR not found." "WARN"
    $python = Install-Python
}
Log "Using Python: $python"

# ---------------------------------------------------------------------------
# 2. Virtual environment
# ---------------------------------------------------------------------------

if (Test-Path $PYTHON_VENV) {
    Log "Virtual environment already exists at $VENV — skipping create."
} else {
    Log "Creating virtual environment at $VENV ..."
    & $python -m venv $VENV
    if (-not (Test-Path $PYTHON_VENV)) { Fail "venv creation did not produce $PYTHON_VENV" }
}

# ---------------------------------------------------------------------------
# 3. Pip + dependencies
# ---------------------------------------------------------------------------

Log "Upgrading pip / setuptools / wheel ..."
& $PYTHON_VENV -m pip install --upgrade pip setuptools wheel --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed (exit $LASTEXITCODE)" }

if (-not (Test-Path $REQUIREMENTS)) {
    Fail "requirements.txt not found at $REQUIREMENTS"
}

Log "Installing Python dependencies from requirements.txt ..."
& $PYTHON_VENV -m pip install -r $REQUIREMENTS --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip install -r requirements.txt failed (exit $LASTEXITCODE)" }

Log "Installing ATLAS package in editable mode ..."
& $PYTHON_VENV -m pip install -e $ATLAS_ROOT --quiet
if ($LASTEXITCODE -ne 0) { Log "pip install -e . returned $LASTEXITCODE (non-fatal)" "WARN" }

# ---------------------------------------------------------------------------
# 4. Database initialisation
# ---------------------------------------------------------------------------

Log "Initialising database schema and seed data ..."
$env:PYTHONPATH = $ATLAS_ROOT
& $PYTHON_VENV -m atlas init-db
if ($LASTEXITCODE -ne 0) { Fail "atlas init-db failed (exit $LASTEXITCODE)" }

# ---------------------------------------------------------------------------
# 5. Windows Firewall rule for port 5000
# ---------------------------------------------------------------------------

Log "Configuring Windows Firewall — inbound TCP 5000 ..."
$ruleName = "ATLAS Dashboard (TCP 5000)"
try {
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
        Log "Firewall rule already exists."
    } else {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound `
            -Action Allow -Protocol TCP -LocalPort 5000 -Profile Any | Out-Null
        Log "Firewall rule created."
    }
} catch {
    Log "Firewall configuration failed (non-fatal): $($_.Exception.Message)" "WARN"
}

# ---------------------------------------------------------------------------
# 6. Desktop and Start Menu shortcuts
# ---------------------------------------------------------------------------

function Make-Shortcut {
    param(
        [string]$Path,
        [string]$Target,
        [string]$Arguments,
        [string]$WorkingDir,
        [string]$IconLocation
    )
    $wsh = New-Object -ComObject WScript.Shell
    $sc = $wsh.CreateShortcut($Path)
    $sc.TargetPath  = $Target
    $sc.Arguments   = $Arguments
    $sc.WorkingDirectory = $WorkingDir
    if ($IconLocation) { $sc.IconLocation = $IconLocation }
    $sc.WindowStyle = 7   # minimized
    $sc.Save()
}

$desktop = [Environment]::GetFolderPath("Desktop")
$startMenu = Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "ATLAS"
Ensure-Dir $startMenu

$startBat = Join-Path $ATLAS_ROOT "start_atlas.bat"
$openBat  = Join-Path $ATLAS_ROOT "open_dashboard.bat"

Log "Creating desktop shortcuts ..."
Make-Shortcut -Path (Join-Path $desktop "Start ATLAS Observatory.lnk") `
              -Target $startBat -Arguments "" -WorkingDir $ATLAS_ROOT
Make-Shortcut -Path (Join-Path $desktop "Open ATLAS Dashboard.lnk") `
              -Target $openBat  -Arguments "" -WorkingDir $ATLAS_ROOT

Log "Creating Start Menu entries ..."
Make-Shortcut -Path (Join-Path $startMenu "Start ATLAS Observatory.lnk") `
              -Target $startBat -Arguments "" -WorkingDir $ATLAS_ROOT
Make-Shortcut -Path (Join-Path $startMenu "Open ATLAS Dashboard.lnk") `
              -Target $openBat  -Arguments "" -WorkingDir $ATLAS_ROOT

# ---------------------------------------------------------------------------
# 7. Optional tools scan
# ---------------------------------------------------------------------------

Log "Scanning for optional third-party tools..."
$missing = @()
foreach ($t in $OPTIONAL_TOOLS) {
    $found = $false
    if ($t.Path -and (Test-Path $t.Path)) { $found = $true }
    if ($t.RegKey -and (Test-Path $t.RegKey)) { $found = $true }
    if ($found) {
        Log ("  [OK]  {0}" -f $t.Name)
    } else {
        Log ("  [--]  {0} not detected — install from {1}" -f $t.Name, $t.Hint) "WARN"
        $missing += $t.Name
    }
}

# ---------------------------------------------------------------------------
# 8. Done
# ---------------------------------------------------------------------------

Log "================================================================"
Log "  ATLAS installation COMPLETE."
Log ""
Log "  Two desktop shortcuts have been created:"
Log "    * Start ATLAS Observatory   (launches the server)"
Log "    * Open ATLAS Dashboard      (opens the dashboard in browser)"
Log ""
Log "  Dashboard URL once running:"
Log "    http://localhost:5000  (this PC)"
Log "    http://<LAN-IP>:5000   (warm room)"
Log ""
if ($missing.Count -gt 0) {
    Log "  Optional tools not yet installed:" "WARN"
    foreach ($m in $missing) { Log ("    - " + $m) "WARN" }
    Log "  ATLAS will operate without them, but several workflows require them." "WARN"
    Log ""
}
Log "  Next: launch ATLAS and complete the Setup tab (master password,"
Log "  Anthropic API key, site coords, NINA/PHD2 hosts, equipment)."
Log "================================================================"

exit 0
