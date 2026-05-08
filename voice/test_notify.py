"""Test ATLAS local notification — run this to verify notifications work."""
import subprocess

def send_windows_toast(title: str, message: str) -> str:
    t = title.replace('"', '').replace("'", '')
    m = message.replace('"', '').replace("'", '')
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        f'$n.BalloonTipTitle = "{t}"; '
        f'$n.BalloonTipText = "{m}"; '
        "$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info; "
        "$n.Visible = $true; "
        "$n.ShowBalloonTip(8000); "
        "Start-Sleep -Seconds 4; "
        "$n.Dispose()"
    )
    result = subprocess.run(
        ["powershell", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        return "Notification sent."
    return f"Failed: {result.stderr.strip()}"

print(send_windows_toast("ATLAS Observatory", "Notifications working. No internet required."))
