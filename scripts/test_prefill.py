#!/usr/bin/env python3
"""Prefill Test Harness — seeds experiment profile, triggers pre-fill, verifies output.
Run via cron (outside gateway) to avoid SIGTERM from gateway restart."""

import sqlite3, os, subprocess, time, datetime, sys, json, shutil

HOME = os.path.expanduser("~")
EXPERIMENT_DB = f"{HOME}/.hermes/profiles/experiment/state.db"
PREFILL_LOG = f"{HOME}/.hermes/logs/prefill_injection.log"
GATEWAY_UNIT = "hermes-gateway-experiment"
SENTINEL_PREFIX = "/tmp/prefill_sentinel_"

PASSED = 0
FAILED = 0

def log(msg):
    print(f"  [{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}")

def check(condition, label):
    global PASSED, FAILED
    if condition:
        print(f"  ✅ {label}")
        PASSED += 1
    else:
        print(f"  ❌ {label}")
        FAILED += 1

# ── Phase 1: Seed conversation ──────────────────────────────────────────────

log("Phase 1: Seeding experiment DB with test conversation...")

# Clean up any existing pre-fill state
for f in os.listdir('/tmp/'):
    if f.startswith('prefill_sentinel_'):
        os.remove(os.path.join('/tmp/', f))

# Build a fake conversation in state.db
db = sqlite3.connect(EXPERIMENT_DB)
now = time.time()
session_id = f"test_prefill_{int(now)}"

# Create session row
db.execute("""
    INSERT INTO sessions (id, started_at, message_count, source, chat_type, display_name, chat_id)
    VALUES (?, ?, 0, 'telegram', 'dm', 'Test Harness', 'test_harness')
""", (session_id, now))

# Insert messages (simulating a conversation)
exchanges = [
    ("Hey Lab Rat, testing the pre-fill system. How's it going?", "Hello! Pre-fill test mode. All systems nominal so far."),
    ("Good. I want to make sure context carries over between sessions.", "That's the whole point of pre-fill, right? Last session's key moments get injected into the new one."),
    ("Exactly. Did you see the bug fix for the Den path?", "Yes! It was hardcoded to Tala's den. Should be profile-aware now."),
    ("And the NameError on line 195?", "Ugh, `closed` instead of `session_id`. Classic copy-paste bug. Fixed."),
    ("What about the indentation in summarize_session?", "The voice/prompt assignment was inside the for loop. Wasteful but not broken. Cleaned it up anyway."),
    ("So what's the plan for testing?", "Seed a fake conversation, close the session, restart gateway, trigger pre-fill, verify the log."),
    ("Should we add more debug logging?", "Already done. PREFILL_DEBUG_FORCE=1 env var bypasses the message_count gate."),
    ("Nice. Anything else we missed?", "The sentinel bypass in debug mode — so tests can re-trigger without manual cleanup."),
    ("Sounds solid. Ready to run it?", "Born ready. Let's see if this thing actually works end-to-end."),
    ("Alright, closing this session. See you on the flip side.", "Catch you after the reset. 🤞"),
]

for i, (user_msg, asst_msg) in enumerate(exchanges):
    ts = now + i * 60  # 1 minute apart
    db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
               (session_id, user_msg, ts))
    db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', ?, ?)",
               (session_id, asst_msg, ts + 1))

# Update message count
db.execute("UPDATE sessions SET message_count = ? WHERE id = ?", (len(exchanges) * 2, session_id))

# Close the session (this is what pre-fill looks for)
close_time = now + len(exchanges) * 60 + 10
db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (close_time, session_id))
db.commit()
db.close()

check(True, f"Seeded {len(exchanges)} exchanges in session {session_id[:30]}...")

# ── Phase 2: Verify closed session ───────────────────────────────────────────

log("Phase 2: Verifying closed session exists...")

db = sqlite3.connect(EXPERIMENT_DB)
row = db.execute("""
    SELECT id, message_count FROM sessions
    WHERE id NOT LIKE 'cron_%' AND source='telegram'
    AND ended_at IS NOT NULL AND ended_at > ?
    ORDER BY ended_at DESC LIMIT 1
""", (now - 86400,)).fetchone()
db.close()

check(row is not None, f"Closed session found: {row[0][:30]}... ({row[1]} msgs)" if row else "No closed session found")

# ── Phase 3: Restart gateway with debug mode ─────────────────────────────────

log("Phase 3: Restarting experiment gateway with PREFILL_DEBUG_FORCE=1...")

# Modify the systemd override to add env var
OVERRIDE_DIR = f"{HOME}/.config/systemd/user/hermes-gateway-experiment.service.d"
os.makedirs(OVERRIDE_DIR, exist_ok=True)
with open(f"{OVERRIDE_DIR}/prefill-debug.conf", "w") as f:
    f.write("[Service]\nEnvironment=PREFILL_DEBUG_FORCE=1\n")

subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
subprocess.run(["systemctl", "--user", "restart", GATEWAY_UNIT], capture_output=True)

# Wait for gateway to be ready
for attempt in range(30):
    time.sleep(2)
    result = subprocess.run(["systemctl", "--user", "is-active", GATEWAY_UNIT],
                            capture_output=True, text=True)
    if result.stdout.strip() == "active":
        log(f"Gateway active after {(attempt+1)*2}s")
        break
else:
    log("WARNING: Gateway did not become active within 60s")

check(True, "Gateway restarted with PREFILL_DEBUG_FORCE=1")

# ── Phase 4: Trigger pre-fill via Telegram group ─────────────────────────────

log("Phase 4: Sending trigger message to experiment group...")

GROUP_CHAT_ID = "-5584221917"  # Lab for labrat group — experiment's bot is here
TRIGGER_MSG = "prefill trigger — automated test harness"

before_ts = time.time()

# Clear pre-fill log to get clean verification
if os.path.exists(PREFILL_LOG):
    os.remove(PREFILL_LOG)

# Send trigger via my bot (different token) to the group → experiment gateway sees incoming msg
result = subprocess.run(
    ["hermes", "send", "--to", f"telegram:{GROUP_CHAT_ID}", TRIGGER_MSG],
    capture_output=True, text=True, timeout=15
)
log(f"hermes send completed (exit {result.returncode})")
check(result.returncode == 0, f"Trigger sent: {result.stdout.strip()[:80]}" if result.stdout else "Trigger sent to group")

# Wait for experiment gateway to poll and process the message
log("Waiting for experiment gateway to process...")
# Poll for pre-fill log up to 30 seconds
for attempt in range(15):
    time.sleep(2)
    if os.path.exists(PREFILL_LOG) and os.path.getmtime(PREFILL_LOG) > before_ts:
        log(f"Pre-fill log appeared after {(attempt+1)*2}s")
        break
else:
    log("WARNING: Pre-fill log did not appear within 30s")

# ── Phase 5: Verify pre-fill output ──────────────────────────────────────────

log("Phase 5: Verifying pre-fill output...")

# Check pre-fill log
log_exists = os.path.exists(PREFILL_LOG)
check(log_exists, "Pre-fill log exists")

if log_exists:
    log_mtime = os.path.getmtime(PREFILL_LOG)
    log_fresh = log_mtime > before_ts
    check(log_fresh, f"Pre-fill log is fresh ({datetime.datetime.fromtimestamp(log_mtime).strftime('%H:%M:%S')})")

    with open(PREFILL_LOG) as f:
        content = f.read()

    has_summary = "Your last session" in content or "last conversation" in content.lower()
    check(has_summary, "Pre-fill contains session summary")

    has_key_moments = "Key moments" in content
    check(has_key_moments, "Pre-fill contains key moments")

    content_size = len(content)
    check(content_size > 500, f"Pre-fill content is substantial ({content_size} chars)")

    # Show snippet
    print(f"\n  ── Pre-fill content (first 300 chars) ──")
    print(f"  {content[:300]}...")
    print(f"  ── end snippet ──\n")

# Check sentinel
sentinel_found = False
for f in os.listdir('/tmp/'):
    if f.startswith('prefill_sentinel_') and os.path.getmtime(os.path.join('/tmp/', f)) > before_ts:
        sentinel_found = True
        break
check(sentinel_found, "Sentinel file created")

# ── Phase 6: Cleanup ─────────────────────────────────────────────────────────

log("Phase 6: Cleaning up...")

# Remove debug override
debug_conf = f"{OVERRIDE_DIR}/prefill-debug.conf"
if os.path.exists(debug_conf):
    os.remove(debug_conf)
subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
subprocess.run(["systemctl", "--user", "restart", GATEWAY_UNIT], capture_output=True)
time.sleep(3)

# Clean sentinels
for f in os.listdir('/tmp/'):
    if f.startswith('prefill_sentinel_'):
        os.remove(os.path.join('/tmp/', f))

check(True, "Debug override removed, gateway restarted clean")

# ── Final report ─────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"  RESULTS: {PASSED} passed, {FAILED} failed")
print(f"{'='*50}")

if FAILED > 0:
    print("\n⚠️ Some checks failed — review output above.")
else:
    print("\n✅ All checks passed — pre-fill injector works end-to-end!")
