from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _escaped(value: str) -> str:
    return value.replace("'", "''")


def _run_powershell(script: str) -> bool:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def send_windows_notification(title: str, message: str) -> bool:
    escaped_title = _escaped(title)
    escaped_message = _escaped(message)
    balloon_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.BalloonTipTitle = '{escaped_title}'
$notify.BalloonTipText = '{escaped_message}'
$notify.Visible = $true
$notify.ShowBalloonTip(10000)
Start-Sleep -Seconds 6
$notify.Dispose()
"""
    if _run_powershell(balloon_script):
        return True
    popup_script = f"""
$wshell = New-Object -ComObject WScript.Shell
[void]$wshell.Popup('{escaped_message}', 12, '{escaped_title}', 64)
"""
    if _run_powershell(popup_script):
        return True
    result = subprocess.run(
        [
            "msg",
            "*",
            "/time:20",
            f"{title}: {message}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def pending_notification_path(project_root: Path) -> Path:
    return project_root / "logs" / "pending_notification.json"


def queue_pending_notification(
    project_root: Path,
    *,
    title: str,
    message: str,
) -> Path:
    path = pending_notification_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "title": title,
        "message": message,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def show_pending_notification_if_present(project_root: Path) -> bool:
    path = pending_notification_path(project_root)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return False
    title = str(payload.get("title") or "Quarterly match review is ready")
    message = str(payload.get("message") or "")
    sent = send_windows_notification(title, message)
    if sent:
        path.unlink(missing_ok=True)
    return sent
