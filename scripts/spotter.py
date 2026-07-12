#!/usr/bin/env python3
# Spotter — Overnight health monitor
# Checks logs, cron jobs, GPU, disk, processes, gateway.
# Alerts to home channel. Silent when all healthy.

import subprocess, json, os, datetime, sys

HOME = os.path.expanduser("~")
JOBS_PATH = os.path.join(HOME, ".hermes/cron/jobs.json")
STATUS = {"healthy": True, "issues": []}


# ── helpers ─────────────────────────────────────────────────────────────────

def _load_jobs():
    """Load cron jobs from jobs.json. Returns {} on failure."""
    try:
        if not os.path.exists(JOBS_PATH):
            return {}
        with open(JOBS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _jobs_for_log(log_path):
    """Return list of job dicts that write to this log."""
    log_name = os.path.basename(log_path)
    # Map log filenames to job IDs that write to them
    LOG_TO_JOB_IDS = {
        "tala_reflection.log":   ["76b399ac370b"],
    }
    job_ids = LOG_TO_JOB_IDS.get(log_name, [])
    if not job_ids:
        return []
    data = _load_jobs()
    jobs = data.get("jobs", [])
    return [j for j in jobs if j.get("id") in job_ids]


def _any_job_enabled(log_path):
    """True if at least one cron job writing to this log is enabled."""
    jobs = _jobs_for_log(log_path)
    if not jobs:
        return True  # no mapping = assume enabled (can't verify)
    return any(j.get("enabled", True) for j in jobs)


# ── checks ──────────────────────────────────────────────────────────────────

def check_log(path, name, lookback_hrs=24):
    """Check if log has recent entries. Skips if all writer jobs are paused."""
    if not _any_job_enabled(path):
        return  # paused intentionally, don't flag

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
    """Check last cron action log for TIMEOUT/ERROR."""
    if not _any_job_enabled(log_path):
        return  # paused intentionally, skip
    if not os.path.exists(log_path):
        return
    with open(log_path) as f:
        lines = f.readlines()
    recent = [l for l in lines[-10:] if l.strip()]
    errors = [l for l in recent if "TIMEOUT" in l or "ERROR" in l]
    if errors:
        STATUS["issues"].append(f"{name}: {len(errors)} error/timeout in last 10 entries")
        STATUS["healthy"] = False


def check_cron_errors():
    """Scan enabled cron jobs for last_status == 'error' (script non-zero exit
    or LLM agent failure). Skips paused/disabled jobs — those are intentionally
    parked and their historical errors are noise."""
    data = _load_jobs()
    jobs = data.get("jobs", [])
    for j in jobs:
        if not j.get("enabled", True):
            continue  # paused/disabled = intentionally parked, skip
        status = j.get("last_status", "")
        if status == "error":
            name = j.get("name", j.get("id", "unknown"))
            error_msg = j.get("last_error", "")
            detail = f" (last error: {error_msg[:80]})" if error_msg else ""
            STATUS["issues"].append(f"Cron '{name}': last run errored{detail}")
            STATUS["healthy"] = False


def check_gateway():
    """Check that critical systemd user units are active."""
    units = [
        ("hermes-gateway-tala", "Tala gateway"),
    ]
    for unit, label in units:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip() != "active":
                STATUS["issues"].append(f"{label}: {result.stdout.strip() or 'inactive'}")
                STATUS["healthy"] = False
        except Exception as e:
            STATUS["issues"].append(f"{label}: check failed ({e})")
            STATUS["healthy"] = False


def check_gpu():
    """Check GPU temps and memory."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
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
        STATUS["healthy"] = False


def check_disk():
    """Check disk space on home and external mounts."""
    mounts = [
        (HOME, "Home"),
        ("/mnt/Storage", "Storage"),
        ("/mnt/LargeStorage", "LargeStorage"),
    ]
    for mount, label in mounts:
        if not os.path.exists(mount):
            continue  # mount may not be present, skip
        try:
            result = subprocess.run(["df", "-h", mount], capture_output=True, text=True, timeout=5)
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 5:
                    pct = int(parts[4].replace("%", ""))
                    if pct > 90:
                        STATUS["issues"].append(f"Disk {label}: {pct}% used")
                        STATUS["healthy"] = False
        except Exception:
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


# ── run all checks ──────────────────────────────────────────────────────────

for log, name in [
    ("/home/cflux/.hermes/logs/tala_reflection.log", "Reflection pipeline"),
]:
    check_log(log, name)

check_cron_errors()
check_gateway()
check_gpu()
check_disk()
check_processes()

# ── report ──────────────────────────────────────────────────────────────────

now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
if STATUS["healthy"]:
    pass  # silent — no output, no delivery
else:
    print(f"⚠️ {now} — {len(STATUS['issues'])} issues detected:\n" +
          "\n".join(f"  - {i}" for i in STATUS["issues"]))
