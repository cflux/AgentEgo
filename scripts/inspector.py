#!/usr/bin/env python3
"""
Code Inspector v2 — two-phase review via local Qwen3-Coder 30B.

Phase 1 (Discovery): Multiple narrow, focused prompts per category.
Phase 2 (Verification): Every finding gets a FRESH context prompt asking
    "Is this ACTUALLY a problem?" Only confirmed issues survive.

Surviving issues get rolled up to Becca for final vetting.

Usage: python3 inspector.py [file1] [file2] ...
       python3 inspector.py --scripts
"""

import subprocess
import json
import os
import sys
import urllib.request
import argparse
import concurrent.futures
import time

CEDAR_OLLAMA = "http://192.168.1.74:11434/api/generate"
MODEL = "qwen3-coder:30b"
MAX_CHARS_PER_FILE = 8000
TIMEOUT = 90  # seconds per API call
DISCOVERY_CONCURRENCY = 4  # parallel discovery passes
VERIFY_CONCURRENCY = 6     # parallel verification calls


# ─── helpers ──────────────────────────────────────────────────────────────────

def read_file(path, max_chars=MAX_CHARS_PER_FILE):
    if not os.path.exists(path):
        return f"[MISSING: {path}]"
    with open(path) as f:
        content = f.read()
    if len(content) > max_chars:
        content = content[:max_chars] + "\n... [truncated]"
    return content


def call_ollama(prompt, label="", timeout=TIMEOUT):
    """Single Ollama API call. Returns response text or error string."""
    req = urllib.request.Request(
        CEDAR_OLLAMA,
        data=json.dumps({
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 512}
        }).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read())
        return result.get("response", "No response").strip()
    except Exception as e:
        return f"[ERROR: {e}]"


def parse_findings(text):
    """
    Parse model output into structured findings.
    Looks for lines matching: FILE:LINE | SEVERITY | description
    or numbered items with file references.
    """
    findings = []
    if not text or "no issues" in text.lower():
        return findings

    for line in text.split("\n"):
        # Strip markdown formatting and whitespace
        line = line.strip().lstrip("- *").strip()
        # Strip backtick-wrapped paths: `/path/file.py:123` → /path/file.py:123
        line = line.replace("`", "")
        if not line or line.startswith("#") or line.startswith("="):
            continue
        # Match patterns like: file.py:123 or /path/file.py:45
        if ":" in line and not line.startswith("http"):
            # Try to extract file:line
            parts = line.split()
            for i, part in enumerate(parts):
                if ".py:" in part or ".sh:" in part:
                    file_line = part.rstrip(",;").strip("`")
                    severity = "MEDIUM"
                    desc = " ".join(parts[i+1:]) if i+1 < len(parts) else ""
                    for s in ["HIGH", "MEDIUM", "LOW"]:
                        if s in line.upper():
                            severity = s
                            break
                    findings.append({
                        "file_line": file_line,
                        "severity": severity,
                        "description": desc[:200],
                        "raw_line": line[:300]
                    })
    return findings


# ─── Phase 1: Discovery ──────────────────────────────────────────────────────

def discover_imports(files_content):
    """Check for missing imports, undefined names, syntax issues."""
    prompt = f"""You are a Python linter. Your ONLY job is to find:

1. Names used but never imported or defined (e.g., a function called but not imported)
2. Imported names that don't exist in their source module

RULES:
- If `import os` is present, `os.path.join` IS available. Do NOT flag it.
- If `import json` is present, `json.loads` IS available. Do NOT flag it.
- Standard library modules are always available.
- If you're NOT CERTAIN something is wrong, do NOT report it.
- Maximum 3 findings total. If nothing is clearly wrong, say "No issues found."

Files:
{files_content}

Format: file:line | SEVERITY | what's wrong (one sentence). ONLY report if certain."""

    return call_ollama(prompt, label="discover_imports")


def discover_paths(files_content):
    """Check for stale/hardcoded paths that might not exist."""
    prompt = f"""You are checking for stale or broken file paths. Your ONLY job:

Look for hardcoded paths (like "/home/user/something" or "~/.config/thing") and evaluate whether they look reasonable for a Linux system running Hermes Agent.

Do NOT flag:
- Standard Linux paths (/tmp, /dev/null, /proc, /sys)
- Python module paths (they resolve via PYTHONPATH, not filesystem)
- os.path.expanduser() or os.path.join() patterns (they're dynamic)
- Hermes standard paths (~/.hermes/, ~/.config/hermes/)

Only flag if:
- A path clearly references a file/directory that has been renamed or removed
- A path uses obsolete Hermes conventions (check against current docs: paths use ~/.hermes/ not ~/.hermes_agent/)

If nothing is clearly stale, say "No issues found."

Files:
{files_content}

Format: file:line | SEVERITY | what path + why suspicious. Max 2 findings."""

    return call_ollama(prompt, label="discover_paths")


def discover_shell(files_content):
    """Check shell scripts for dangerous patterns."""
    # Only run if there are .sh files
    if ".sh" not in files_content and "#!/bin/bash" not in files_content:
        return "No issues found."

    prompt = f"""You are reviewing shell scripts for safety issues. Your ONLY job:

Check for:
1. Unquoted variable expansions that could break on spaces/special chars
2. Dangerous commands: rm -rf without safeguards, eval with user input, curl piped to bash
3. Missing error handling: set -e or || exit patterns

Do NOT flag:
- Standard boilerplate (set -euo pipefail, shebangs)
- Well-quoted variables
- Commands that are clearly intentional and safe in context

If nothing is dangerous, say "No issues found."

Files:
{files_content}

Format: file:line | SEVERITY | what's dangerous + why. Max 2 findings."""

    return call_ollama(prompt, label="discover_shell")


def discover_logic(files_content):
    """Check for race conditions, double-injection, missing guards."""
    prompt = f"""You are reviewing Python code for logic bugs. Your ONLY job:

Check ONE thing: race conditions and missing guards.

Specifically look for:
1. Check-then-write patterns (os.path.exists() then open() — two processes can race)
2. Missing sentinel/cooldown guards that could cause double-execution
3. File operations without atomic primitives (os.O_CREAT|os.O_EXCL instead of exists+open)

Do NOT flag:
- Single-threaded operations (if there's no concurrency, there's no race)
- Patterns that are clearly intentional
- Anything you're not CERTAIN about

If no clear race conditions exist, say "No issues found."

Files:
{files_content}

Format: file:line | SEVERITY | what's racy + why. Max 2 findings. ONLY if certain."""

    return call_ollama(prompt, label="discover_logic")


def discover_config(files_content):
    """Check for Hermes config drift."""
    prompt = f"""You are checking for deprecated or wrong Hermes Agent configuration patterns.

Current Hermes conventions (as of July 2026):
- Config lives at ~/.hermes/config.yaml
- Profiles at ~/.hermes/profiles/<name>/
- Plugins at ~/.hermes/plugins/ (or per-profile plugins/)
- State DB is at ~/.hermes/state.db or per-profile
- Environment: HERMES_HOME points to ~/.hermes/
- Skills at ~/.hermes/skills/

Flag ONLY if:
- Code uses clearly wrong/old paths (like ~/.hermes_agent/ — that's v0.17)
- References environment variables that don't exist in current Hermes

If config references look correct, say "No issues found."

Files:
{files_content}

Format: file:line | SEVERITY | what's wrong + correct value. Max 1 finding."""

    return call_ollama(prompt, label="discover_config")


# ─── Phase 2: Verification ───────────────────────────────────────────────────

def verify_finding(file_paths, finding, debug=False):
    """
    Fresh context: ask the model to verify ONE specific finding.
    Returns (confirmed: bool, explanation: str)
    """
    file_line = finding["file_line"]
    # Extract just the file path from "file:line"
    if ":" in file_line:
        parts = file_line.rsplit(":", 1)
        fname = parts[0]
        try:
            line_num = int(parts[1])
        except ValueError:
            line_num = None
    else:
        fname = file_line
        line_num = None

    # Match file: try exact match first, then basename, then substring
    matched_path = None
    for fp in file_paths:
        if fp == fname or fp.endswith("/" + fname.split("/")[-1]) or fname.split("/")[-1] in fp:
            matched_path = fp
            break
    if not matched_path:
        # Last resort: match by basename only
        target_base = os.path.basename(fname)
        for fp in file_paths:
            if os.path.basename(fp) == target_base:
                matched_path = fp
                break

    if not matched_path:
        if debug:
            print(f"    [DEBUG verify] Could not locate: fname='{fname}' in {[os.path.basename(f) for f in file_paths]}")
        return (False, f"Could not locate file: {fname}")

    content = read_file(matched_path, max_chars=4000)

    # Build a tight verification prompt
    context = ""
    if line_num:
        # Show surrounding lines for context
        lines = content.split("\n")
        start = max(0, line_num - 5)
        end = min(len(lines), line_num + 5)
        context = f"Relevant code (around line {line_num}):\n```\n"
        for i in range(start, end):
            marker = ">>> " if i == line_num - 1 else "    "
            context += f"{marker}{i+1}: {lines[i]}\n"
        context += "```\n"
    else:
        context = f"File content:\n```\n{content[:3000]}\n```\n"

    prompt = f"""You are a code reviewer verifying a potential bug report. Read the code carefully.

CLAIM: {finding['description']}
FILE: {matched_path}
{f'LINE: {line_num}' if line_num else ''}
SEVERITY: {finding['severity']}

{context}

QUESTION: Is this ACTUALLY a bug? Answer YES or NO, then explain in one sentence.

RULES:
- If the code is correct and the claim is wrong → NO
- If you can't find evidence for the claim → NO
- Only say YES if you can point to the exact code that's wrong
- Do NOT guess. If uncertain, say NO."""

    response = call_ollama(prompt, label=f"verify_{file_line}", timeout=45)
    confirmed = response.strip().upper().startswith("YES")
    return (confirmed, response[:300])


# ─── Main pipeline ────────────────────────────────────────────────────────────

def review_files(file_paths, debug=False):
    """Two-phase review: discovery → verification → rollup."""
    files_content = ""
    for fp in file_paths:
        tag = "[SHELL]" if fp.endswith(".sh") else "[PYTHON]"
        files_content += f"\n### {tag} {fp}\n```\n{read_file(fp)}\n```\n"

    # ── Phase 1: Discovery (parallel) ──────────────────────────────────────
    print(f"Phase 1: Discovery — {len(file_paths)} files across 5 focused passes...")
    print(f"  Model: {MODEL} @ {CEDAR_OLLAMA}\n")

    discovery_tasks = {
        "imports": discover_imports,
        "paths": discover_paths,
        "shell": discover_shell,
        "logic": discover_logic,
        "config": discover_config,
    }

    all_raw_findings = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=DISCOVERY_CONCURRENCY) as executor:
        futures = {}
        for name, func in discovery_tasks.items():
            futures[executor.submit(func, files_content)] = name

        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = f"[ERROR: {e}]"

            findings = parse_findings(result)
            status = f"{len(findings)} finding(s)" if findings else "clean"
            print(f"  [{name:8s}] {status}")
            if debug and findings:
                for f in findings:
                    print(f"           RAW: {f['raw_line'][:120]}")

            # Tag findings with their discovery pass
            for f in findings:
                f["source"] = name
            all_raw_findings.extend(findings)

    total_raw = len(all_raw_findings)
    print(f"\n  Discovery complete: {total_raw} raw finding(s)\n")

    if total_raw == 0:
        return "\n✅ No issues found in any discovery pass."

    # ── Phase 2: Verification (parallel per finding) ───────────────────────
    print(f"Phase 2: Verification — checking each finding with fresh context...\n")

    confirmed = []
    dismissed = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=VERIFY_CONCURRENCY) as executor:
        futures = {}
        for i, finding in enumerate(all_raw_findings):
            futures[executor.submit(verify_finding, file_paths, finding, debug)] = (i, finding)

        for future in concurrent.futures.as_completed(futures):
            idx, finding = futures[future]
            try:
                is_real, explanation = future.result()
            except Exception as e:
                is_real, explanation = False, f"[ERROR: {e}]"

            tag = finding["file_line"]
            if is_real:
                confirmed.append(finding)
                print(f"  ✅ CONFIRMED: {tag} — {explanation[:100]}")
            else:
                dismissed.append(finding)
                print(f"  ❌ DISMISSED: {tag} — {explanation[:100]}")

    print(f"\n  Verification complete: {len(confirmed)} confirmed, {len(dismissed)} dismissed\n")

    # ── Rollup ─────────────────────────────────────────────────────────────
    if not confirmed:
        return f"✅ All {total_raw} inspector flags dismissed as false positives. No real issues."

    report = []
    report.append("=" * 60)
    report.append("### Issues Found (verified)")
    report.append("")

    for i, f in enumerate(confirmed, 1):
        report.append(f"#### {i}. **{f.get('source', 'unknown').title()}**")
        report.append(f"- **File**: `{f['file_line'].split(':')[0] if ':' in f['file_line'] else f['file_line']}`")
        if ':' in f['file_line']:
            report.append(f"- **Line**: {f['file_line'].rsplit(':', 1)[1]}")
        report.append(f"- **Severity**: {f['severity']}")
        report.append(f"- **What's wrong**: {f['description']}")
        report.append("")

    report.append(f"---")
    report.append(f"**{len(confirmed)} confirmed, {len(dismissed)} dismissed** "
                  f"(out of {total_raw} raw flags)")
    report.append("")

    return "\n".join(report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Code Inspector v2 — two-phase review')
    parser.add_argument('files', nargs='*', help='Files to review')
    parser.add_argument('--scripts', action='store_true',
                        help='Review all ~/.hermes/scripts/')
    parser.add_argument('--single-pass', action='store_true',
                        help='Legacy mode: single prompt (skip two-phase)')
    parser.add_argument('--debug', action='store_true',
                        help='Show raw model outputs during discovery/verification')
    args = parser.parse_args()

    if args.scripts:
        scripts_dir = os.path.expanduser('~/.hermes/scripts')
        args.files = [os.path.join(scripts_dir, f)
                      for f in os.listdir(scripts_dir)
                      if f.endswith(('.sh', '.py')) and f != 'inspector.py']

    if not args.files:
        print("No files to review. Pass file paths or use --scripts.")
        sys.exit(1)

    if args.single_pass:
        # Legacy mode fallback
        files_content = ""
        for fp in args.files:
            files_content += f"\n### {fp}\n```\n{read_file(fp)}\n```\n"
        prompt = f"""Review these files for issues. Flag ONLY real problems:
1. Missing imports or undefined names
2. Stale/broken file paths
3. Shell hazards (unquoted vars, dangerous commands)
4. Race conditions or missing guards
5. Deprecated Hermes config keys

For each: file:line, severity, what's wrong, fix. Be specific. If uncertain, do NOT report."""
        print(f"Legacy single-pass review of {len(args.files)} files...\n")
        result = call_ollama(prompt + "\n\nFiles:\n" + files_content)
        print("=" * 60)
        print(result)
    else:
        report = review_files(args.files, debug=args.debug)
        print(report)
