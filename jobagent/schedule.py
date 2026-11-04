"""Install a macOS launchd agent that runs the pipeline every morning.

launchd (not cron) because it catches up on a run missed while the Mac was asleep.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import ROOT

LABEL = "com.fazil.jobagent"

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{uv}</string><string>run</string><string>--project</string><string>{root}</string>
    <string>jobagent</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path}</string>
    <key>HOME</key><string>{home}</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>StandardOutPath</key><string>{root}/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>{root}/logs/launchd.err.log</string>
</dict>
</plist>
"""


def install(hour: int = 7, minute: int = 30) -> str:
    uv = shutil.which("uv")
    if not uv:
        return "uv not found on PATH. Install it: brew install uv"
    # launchd starts with a bare PATH; carry over the one that finds claude, pdflatex, uv.
    path = os.environ.get("PATH", "/usr/bin:/bin")
    for extra in ("/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin")):
        if extra not in path.split(":"):
            path += f":{extra}"
    plist = PLIST.format(label=LABEL, uv=uv, root=ROOT, path=path, home=Path.home(), hour=hour, minute=minute)
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    target.write_text(plist)
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
    res = subprocess.run(["launchctl", "load", str(target)], capture_output=True, text=True)
    if res.returncode != 0:
        return f"wrote {target} but launchctl load failed: {res.stderr.strip()}"
    return f"Scheduled daily at {hour:02d}:{minute:02d} ({target}). Logs: {ROOT}/logs/"


def uninstall() -> str:
    target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if not target.exists():
        return "No launchd job installed; nothing to remove."
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True)
    target.unlink()
    return f"Removed the daily launchd job ({target})."
