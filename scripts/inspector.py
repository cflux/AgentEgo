#!/usr/bin/env python3
"""
Code Inspector — on-demand code review via Cedar's Qwen3-Coder 30B.
Usage: python3 inspector.py [file1] [file2] ...
"""
import subprocess, json, os, sys, urllib.request, argparse

CEDAR_OLLAMA = "http://192.168.1.74:11434/api/generate"
MODEL = "qwen3-coder:30b"
MAX_CHARS_PER_FILE = 8000  # keep prompts manageable
EXCLUDE_DIRS = ['mnemosyne', '__pycache__']  # skip plugins we didn't author


def read_file(path, max_chars=MAX_CHARS_PER_FILE):
    """Read file, truncate if too long."""
    if not os.path.exists(path):
        return f"[MISSING: {path}]"
    with open(path) as f:
        content = f.read()
    if len(content) > max_chars:
        content = content[:max_chars] + "\n... [truncated]"
    return content


def review_files(file_paths):
    """Send files to Qwen3-Coder for review."""
    files_content = ""
    for fp in file_paths:
        files_content += f"\n### {fp}\n```\n{read_file(fp)}\n```\n"
    
    prompt = f"""Review these scripts/configs for issues. Flag ONLY real problems:

1. Stale file paths — does the path exist on this system?
2. Unreferenced or misspelled variables
3. Logic bugs — double-injection, missing cooldowns, race conditions
4. Shell hazards — unquoted variables, dangerous commands
5. Python issues — missing imports, syntax errors
6. Config drift — Hermes config keys that might be deprecated

For each issue: file:line, severity (HIGH/MEDIUM/LOW), what's wrong, suggested fix.

Files to review:
{files_content}

Respond with issues found (or "No issues found" if clean). Be specific about file:line."""

    req = urllib.request.Request(
        CEDAR_OLLAMA,
        data=json.dumps({
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 1024}
        }).encode(),
        headers={"Content-Type": "application/json"}
    )
    
    print(f"Reviewing {len(file_paths)} files with {MODEL}...")
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read())
    return result.get("response", "No response from model")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Code Inspector')
    parser.add_argument('files', nargs='+', help='Files to review')
    parser.add_argument('--scripts', action='store_true', help='Review all ~/.hermes/scripts/')
    args = parser.parse_args()
    
    if args.scripts:
        scripts_dir = os.path.expanduser('~/.hermes/scripts')
        args.files = [os.path.join(scripts_dir, f) for f in os.listdir(scripts_dir) if f.endswith(('.sh', '.py'))]
    
    report = review_files(args.files)
    print("\n" + "="*60)
    print(report)
