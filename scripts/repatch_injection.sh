#!/usr/bin/env bash
# Re-apply the pre_llm_call context-injection fix to the installed hermes-agent.
#
# WHY THIS EXISTS
#   agent/conversation_loop.py computes `current_turn_user_idx` in the turn prologue, then
#   calls repair_message_sequence_with_cursor() inside the loop. That repair can COMPACT
#   `messages`, leaving the pre-repair index pointing past the end of the shortened list.
#   The injection step is gated on `idx == current_turn_user_idx`, so in any session long
#   enough to trigger repair compaction, every pre_llm_call `{"context": ...}` return
#   (mood, impulse catch-up, time) plus the memory prefetch is SILENTLY DISCARDED.
#
#   Upstream ships the right helper -- reanchor_current_turn_user_idx() in
#   agent/turn_context.py -- but only calls it after a compression restart, never after the
#   repair. So every upgrade reintroduces the bug and this script must be re-run.
#
#   Full writeup: ~/hermes-pre_llm_call-injection-bug.md
#
# USAGE
#   scripts/repatch_injection.sh          # apply (idempotent)
#   scripts/repatch_injection.sh --check  # verify only, non-zero if patch missing
#
# The patch targets the INSTALLED venv copy (site-packages), which is what the gateway
# imports -- never the git checkout in ~/.hermes/hermes-agent/, which is stale and not used.
set -euo pipefail

VENV_PY="${HERMES_VENV_PY:-$HOME/.hermes/hermes-agent/venv/bin/python}"

if [[ ! -x "$VENV_PY" ]]; then
    echo "FATAL: venv interpreter not found at $VENV_PY" >&2
    exit 1
fi

MODE="apply"
if [[ "${1:-}" == "--check" ]]; then
    MODE="check"
fi

# Resolve the real module path from the interpreter itself rather than hardcoding
# .../lib/python3.11/..., so a venv rebuilt on a different Python still works.
TARGET="$("$VENV_PY" - <<'PY'
import importlib.util, sys
spec = importlib.util.find_spec("agent.conversation_loop")
if spec is None or not spec.origin:
    sys.exit("could not locate agent.conversation_loop")
print(spec.origin)
PY
)"

echo "target: $TARGET"

MODE="$MODE" TARGET="$TARGET" "$VENV_PY" - <<'PY'
import os, re, shutil, sys, time

target = os.environ["TARGET"]
mode = os.environ["MODE"]

MARKER = "hermes-injection-reanchor"
ANCHOR = "        repaired_seq = repair_message_sequence_with_cursor(agent, messages)"
INSERT_BEFORE = "        api_messages = []"

PATCH = '''\
        # Local patch (hermes-injection-reanchor): repair_message_sequence_with_cursor
        # (above) can compact `messages`, leaving the pre-repair current_turn_user_idx
        # pointing past the end -- which silently drops every pre_llm_call {"context": ...}
        # return (mood / impulse / time) and the memory prefetch. Upstream defines
        # reanchor_current_turn_user_idx() but only calls it after a compression restart,
        # not here. Re-applied by scripts/repatch_injection.sh after every upgrade.
        # See ~/hermes-pre_llm_call-injection-bug.md
        current_turn_user_idx = reanchor_current_turn_user_idx(messages, user_message)
'''

src = open(target, encoding="utf-8").read()

def compiles(text):
    try:
        compile(text, target, "exec")
        return True
    except SyntaxError as e:
        print(f"  syntax error: line {e.lineno}: {e.msg}", file=sys.stderr)
        return False

if MARKER in src:
    print("status: already patched")
    if not compiles(src):
        sys.exit("FATAL: patched file does not parse")
    print("status: parses OK")
    sys.exit(0)

if mode == "check":
    sys.exit("MISSING: injection re-anchor patch is NOT present -- run without --check")

# The upstream helper must already be imported; it is, at conversation_loop.py:38.
if "reanchor_current_turn_user_idx" not in src:
    sys.exit(
        "FATAL: reanchor_current_turn_user_idx not found in the file. Upstream refactored "
        "the turn-context helpers -- re-derive the fix by hand before trusting this script."
    )

lines = src.splitlines(keepends=True)

# Locate the repair call, then the first `api_messages = []` after it.
try:
    anchor_i = next(i for i, l in enumerate(lines) if l.rstrip("\n") == ANCHOR)
except StopIteration:
    sys.exit(
        "FATAL: anchor not found:\n  " + ANCHOR.strip() + "\n"
        "Upstream moved or renamed the repair call. Do NOT guess -- re-read the injection "
        "guard in run_conversation and re-derive the insertion point."
    )

try:
    insert_i = next(
        i for i in range(anchor_i, min(anchor_i + 40, len(lines)))
        if lines[i].rstrip("\n") == INSERT_BEFORE
    )
except StopIteration:
    sys.exit(
        "FATAL: found the repair call but no `api_messages = []` within 40 lines after it. "
        "The loop structure changed; re-derive the insertion point by hand."
    )

# Strip the older v0.18 hand-patch if it somehow survived (e.g. script run pre-upgrade).
old_start = None
for i in range(anchor_i, insert_i):
    if "v0.18 fix:" in lines[i]:
        old_start = i
        break
if old_start is not None:
    del lines[old_start:insert_i]
    insert_i = old_start
    print("status: removed superseded v0.18 hand-patch")

lines.insert(insert_i, PATCH)
patched = "".join(lines)

if not compiles(patched):
    sys.exit("FATAL: patched source does not parse -- refusing to write")

backup = f"{target}.orig-{time.strftime('%Y%m%dT%H%M%S')}"
shutil.copy2(target, backup)

tmp = target + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(patched)
shutil.copystat(target, tmp)
os.replace(tmp, target)

print(f"status: patched (backup: {os.path.basename(backup)})")
print(f"        inserted at line {insert_i + 1}")
PY

# Drop any stale bytecode so the next import definitely picks up the patched source.
find "$(dirname "$TARGET")/__pycache__" -name 'conversation_loop.*' -delete 2>/dev/null || true

echo "done. verify with: grep -n 'hermes-injection-reanchor' '$TARGET'"
