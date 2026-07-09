#!/usr/bin/env python3
# Spotter — Overnight health monitor
# Checks logs, cron outputs, GPU, disk. Alerts to home channel.

import subprocess, json, os, datetime, sys

HOME = os.path.expanduser("~")
STATUS = {"healthy": True, "issues": []}

def check_log(path, name, lookback_hrs=24):
    """Check if log has entries in the last N hours."""
    if not os.path.exists(path):
        STATUS["issues"].append(f"{name}: log file missing")
        STATUS["healthy"] = False
        return
    mtime = os.path.getmtime(path)
    age = (datetime.datetime.now() - datetime.datetime.fromtimestamp(mtime)).total_seconds() / 3600
    if age > lookback_hrs:
        STATUS["issues"].append(f"{name}: no entries in {age:.0f}h")
        STATUS["healthy"] = False

def check_cron(log_path, name):
    """Check last cron action for errors."""
    if not os.path.exists(log_path):
        return
    with open(log_path) as f:
        lines = f.readlines()
    recent = [l for l in lines[-10:] if l.strip()]
    errors = [l for l in recent if "TIMEOUT" in l or "ERROR" in l]
    if errors:
        STATUS["issues"].append(f"{name}: {len(errors)} errors in last 10 entries")
        STATUS["healthy"] = False

def check_gpu():
    """Check GPU temps and memory."""
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=5)
        for i, line in enumerate(result.stdout.strip().split("\n")):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                temp = int(parts[0].replace("°C","").strip())
                mem_used = int(parts[1].replace("MiB","").strip())
                mem_total = int(parts[2].replace("MiB","").strip())
                pct = mem_used / mem_total * 100 if mem_total else 0
                if temp > 80:
                    STATUS["issues"].append(f"GPU {i}: {temp}°C (high)")
                    STATUS["healthy"] = False
                if pct > 95:
                    STATUS["issues"].append(f"GPU {i}: {pct:.0f}% memory (critical)")
                    STATUS["healthy"] = False
    except Exception as e:
        STATUS["issues"].append(f"GPU check failed: {e}")

def check_disk():
    """Check disk space."""
    try:
        result = subprocess.run(["df", "-h", HOME], capture_output=True, text=True, timeout=5)
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                pct = int(parts[4].replace("%", ""))
                if pct > 90:
                    STATUS["issues"].append(f"Disk: {pct}% used")
                    STATUS["healthy"] = False
    except:
        pass

def check_processes():
    """Check critical processes."""
    critical = {
        "ComfyUI": "ComfyUI",
        "Ollama": "ollama serve",
        "AgentEgo": "sentiment_worker"
    }
    for name, pattern in critical.items():
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
        if result.returncode != 0:
            STATUS["issues"].append(f"{name}: not running")
            STATUS["healthy"] = False

# Run all checks
for log, name in [
    ("/home/cflux/.hermes/logs/tala_session_end.log", "Session detector"),
    ("/home/cflux/.hermes/logs/tala_reflection.log", "Reflection pipeline"),
]:
    check_log(log, name)

for path, name in [
    ("/home/cflux/.hermes/logs/tala_session_end.log", "Session end"),
]:
    check_cron(path, name)

check_gpu()
check_disk()
check_processes()

# Report
now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
if STATUS["healthy"]:
    pass  # silent — no output, no delivery
else:
    print(f"⚠️ {now} — {len(STATUS['issues'])} issues detected:\n" + "\n".join(f"  - {i}" for i in STATUS["issues"]))
