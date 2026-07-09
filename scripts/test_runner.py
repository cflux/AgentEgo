#!/usr/bin/env python3
"""Test Runner — validates all scripts and plugins nightly."""
import subprocess, os, sys, glob

SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
PLUGINS_DIR = os.path.expanduser("~/.hermes/plugins")
EXCLUDE = ['mnemosyne']  # skip built-in plugins

results = {"pass": 0, "fail": 0, "errors": []}

# Test shell scripts
for f in sorted(glob.glob(f"{SCRIPTS_DIR}/*.sh")):
    name = os.path.basename(f)
    result = subprocess.run(["bash", "-n", f], capture_output=True, text=True)
    if result.returncode == 0:
        results["pass"] += 1
    else:
        results["fail"] += 1
        results["errors"].append(f"{name}: {result.stderr.strip()[:120]}")

# Test Python scripts
for f in sorted(glob.glob(f"{SCRIPTS_DIR}/*.py")):
    name = os.path.basename(f)
    result = subprocess.run(["python3", "-m", "py_compile", f], capture_output=True, text=True)
    if result.returncode == 0:
        results["pass"] += 1
    else:
        results["fail"] += 1
        results["errors"].append(f"{name}: {result.stderr.strip()[:120]}")

# Test our plugins (skip mnemosyne)
for plugin_dir in sorted(glob.glob(f"{PLUGINS_DIR}/*/")):
    plugin_name = os.path.basename(plugin_dir.rstrip("/"))
    if plugin_name in EXCLUDE:
        continue
    for f in sorted(glob.glob(f"{plugin_dir}/*.py")):
        name = f"{plugin_name}/{os.path.basename(f)}"
        result = subprocess.run(["python3", "-m", "py_compile", f], capture_output=True, text=True)
        if result.returncode == 0:
            results["pass"] += 1
        else:
            results["fail"] += 1
            results["errors"].append(f"{name}: {result.stderr.strip()[:120]}")

# Report
if results["fail"] == 0:
    pass  # silent — all green
else:
    print(f"🧪 Test Runner: {results['pass']} passed, {results['fail']} failed")
    for e in results["errors"]:
        print(f"  ❌ {e}")
