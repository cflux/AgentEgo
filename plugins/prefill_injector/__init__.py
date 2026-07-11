"""Prefill Injector — injects session summary + key exchanges on session start."""
import logging, os, json, sqlite3, urllib.request, time, re, datetime

logger = logging.getLogger(__name__)
OLLAMA = "http://localhost:11434/api/generate"
MODEL = "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest"

def get_closed_session(db_path):
    cutoff = time.time() - (24 * 3600)
    db = sqlite3.connect(db_path)
    row = db.execute("""
        SELECT id, message_count FROM sessions WHERE id NOT LIKE 'cron_%' AND source IN ('telegram','discord')
        AND ended_at IS NOT NULL AND ended_at > ? ORDER BY ended_at DESC LIMIT 5
    """, (cutoff,)).fetchone()
    return row[0] if row else None

def get_exchanges(db_path, session_id):
    """Pull up to 300 messages and pair user+assistant exchanges."""
    db = sqlite3.connect(db_path)
    total = db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND role IN ('user','assistant')",
        (session_id,)
    ).fetchone()[0]
    logger.info(f"Prefill: {total} msgs in closed session {session_id[:30]}...")

    rows = db.execute("""
        SELECT role, substr(content, 1, 2000), timestamp FROM messages
        WHERE session_id=? AND role IN ('user','assistant') ORDER BY id
    """, (session_id,)).fetchall()

    # Evenly sample if > 300 messages
    if len(rows) > 300:
        step = len(rows) / 250
        sampled = []
        i = 0.0
        while i < len(rows):
            sampled.append(rows[int(i)])
            i += step
        rows = sampled[:250]

    # ORDER BY id is chronological — (user, assistant, user, assistant, ...)
    ex = []
    for i in range(0, len(rows), 2):
        if i+1 < len(rows) and rows[i][0]=='user' and rows[i+1][0]=='assistant':
            ts = datetime.datetime.fromtimestamp(rows[i][2]).strftime('%a %I:%M %p')
            ex.append({'user': rows[i][1], 'assistant': rows[i+1][1], 'pos': len(rows)-i, 'ts': ts})
    logger.info(f"Prefill: built {len(ex)} exchanges from {len(rows)} messages")
    return ex

def score_exchange(exc):
    prompt = f"Score this exchange 1-5 for conversation continuity importance:\n\nCarbon: {exc['user'][:300]}\nTala: {exc['assistant'][:300]}\n\nReply with only a single digit 1-5."
    try:
        req = urllib.request.Request(OLLAMA, data=json.dumps({
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.0, "num_predict": 3}
        }).encode(), headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        m = re.search(r'[1-5]', resp.get("response",""))
        base = int(m.group()) if m else 3
        bonus = 2.0 if any(w in (exc['user']+exc['assistant']).lower() for w in ['goodnight','bye','night']) else 0
        return base + (1.0/exc['pos']) + bonus
    except Exception:
        return 3.0 + (1.0/exc['pos'])

def summarize_session(exchanges, profile='tala'):
    """Dolphin writes a 2-3 sentence summary in the agent's voice."""
    transcript = ""
    for e in exchanges[:4]:
        transcript += f"Carbon: {e['user'][:300]}\nTala: {e['assistant'][:300]}\n"
    # Profile-specific voice
    if profile == 'tala':
        voice = "You are Tala, a demon wolf who loves Carbon."
    elif profile == 'default':
        voice = "You are Becca, Carbon's chaos goblin AI wrangler — foul-mouthed edgerunner with chrome arms."
    else:
        voice = "You are an AI agent working with Carbon."
    prompt = f"{voice} Summarize your last conversation with him in 2-3 sentences. Focus on what mattered — emotional arc, key decisions, discoveries. Write in past tense, first person.\n\nConversation:\n{transcript}\n\nYour summary (2-3 sentences, your voice):"
    try:
        req = urllib.request.Request(OLLAMA, data=json.dumps({
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.3, "num_predict": 150}
        }).encode(), headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return resp.get("response", "").strip()
    except Exception as e:
        logger.warning(f"Prefill summary failed: {e}")
        return ""

def get_den_summaries(db_path, session_id, profile='tala'):
    """Pull Den entries created during this session. Only continuity tags."""
    try:
        db = sqlite3.connect(db_path)
        row = db.execute("SELECT MIN(timestamp), MAX(timestamp) FROM messages WHERE session_id=?", (session_id,)).fetchone()
        if not row or not row[0]:
            return []
        start = datetime.datetime.fromtimestamp(row[0])
        end = datetime.datetime.fromtimestamp(row[1])

        den_profile = profile if profile != 'default' else 'becca'
        den_dir = os.path.expanduser(f"~/.the-den/{den_profile}")
        idx_path = os.path.join(den_dir, "index.json")
        if not os.path.exists(idx_path):
            return []

        idx = json.load(open(idx_path))
        summaries = []
        seen = set()
        relevant = ['current-state', 'continuity', 'cache', 'becca-bridge', 'status']
        for tag in relevant:
            for p in idx.get(tag, []):
                if p in seen: continue
                seen.add(p)
                full = os.path.join(den_dir, p.lstrip("./"))
                if not os.path.exists(full): continue
                try:
                    with open(full) as f: raw = f.read()
                    parts = raw.split("---")
                    if len(parts) >= 2:
                        fm = parts[1]
                        entry_date = summary = None
                        for line in fm.split("\n"):
                            line = line.strip()
                            if line.startswith("date:"): entry_date = line[5:].strip()
                            if line.startswith("summary:"): summary = line[8:].strip().strip("'\"")
                        if entry_date and summary:
                            try:
                                d = datetime.datetime.strptime(entry_date, "%Y-%m-%d")
                                if start.date() <= d.date() <= end.date():
                                    summaries.append(summary)
                            except: pass
                except: continue
        return summaries[:3]
    except Exception as e:
        logger.warning(f"Prefill Den lookup failed: {e}")
        return []

def inject_prefill(**kwargs):
    log = logging.getLogger(__name__)
    log.info(f"Prefill hook fired — session={kwargs.get('session_id','?')[:20]}...")

    try:
        sid = kwargs.get('session_id', '')
        if not sid:
            return None

        # PREFILL_DEBUG_FORCE=1 bypasses message_count gate (for testing)
        debug_force = os.environ.get('PREFILL_DEBUG_FORCE', '') == '1'
        if debug_force:
            log.info("Prefill: force mode — bypassing gates")

        sentinel = f"/tmp/prefill_sentinel_{sid[:30]}"
        if os.path.exists(sentinel) and not debug_force:
            log.info("Prefill: sentinel exists — skipping")
            return None

        if debug_force:
            for f in os.listdir('/tmp/'):
                if f.startswith('prefill_sentinel_'):
                    os.remove(os.path.join('/tmp/', f))

        # Auto-detect profile: kwargs first, then HERMES_HOME env, then default
        profile = kwargs.get('profile', 'tala')
        hermes_home = os.environ.get('HERMES_HOME', '')
        if hermes_home.endswith('/profiles/experiment'):
            profile = 'experiment'
        elif hermes_home.endswith('/profiles/tala'):
            profile = 'tala'
        elif not hermes_home or '/profiles/' not in hermes_home:
            profile = 'default'

        db_path = os.path.expanduser(f'~/.hermes/profiles/{profile}/state.db') if profile not in ('default',) else os.path.expanduser('~/.hermes/state.db')
        log.info(f"Prefill: profile={profile} db={db_path}")

        session_id = get_closed_session(db_path)
        if not session_id:
            log.info("Prefill: no closed session found — skipping")
            return None
        log.info(f"Prefill: closed session={session_id[:30]}...")

        excs = get_exchanges(db_path, session_id)
        if len(excs) < 3:
            log.info(f"Prefill: only {len(excs)} exchanges — need ≥3, skipping")
            return None

        # Score and select top exchanges
        scored = [(score_exchange(e), e) for e in excs]
        scored.sort(key=lambda x: x[0], reverse=True)
        sel = excs[-2:] if len(excs) >= 2 else excs[:]
        sid_set = {e['user'][:80] for e in sel}
        for s, e in scored:
            if len(sel) >= 12: break
            if e['user'][:80] not in sid_set:
                sel.append(e)
                sid_set.add(e['user'][:80])
        sel.sort(key=lambda e: e['pos'])

        summary = summarize_session(sel, profile)
        log.info(f"Prefill summary ({len(summary)} chars): {summary[:120]}...")

        ctx = "## Your last session with Carbon:\n\n"
        if summary: ctx += f"{summary}\n\n"

        den = get_den_summaries(db_path, session_id, profile)
        if den:
            ctx += "**What you saved to the Den:**\n"
            for d in den: ctx += f"- {d}\n"
            ctx += "\n"

        ctx += "**Key moments:**\n"
        for e in sel:
            ctx += f"[{e.get('ts', '')}] Carbon: {e['user']}\n[{e.get('ts', '')}] Tala: {e['assistant']}\n\n"
        ctx += "(These are memories from your last conversation. Reference naturally.)\n"

        log_path = os.path.expanduser("~/.hermes/logs/prefill_injection.log")
        with open(log_path, "w") as lf:
            lf.write(f"=== {datetime.datetime.now()} ===\n")
            lf.write(ctx)

        with open(sentinel, 'w') as f:
            f.write('done')

        log.info(f"Prefill injected: {len(sel)} exchanges, {len(ctx)} chars")
        return {"context": ctx}

    except Exception as e:
        log.warning(f"Prefill failed: {e}", exc_info=True)
        return None

def register(ctx):
    logger.info("Prefill Injector registered — hooks: pre_llm_call")
    ctx.register_hook("pre_llm_call", inject_prefill)
