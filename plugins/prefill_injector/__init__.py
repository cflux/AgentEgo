"""Prefill Injector — injects Tala session summary + key exchanges on session start."""
import logging, os, json, sqlite3, urllib.request, time, re, datetime

logger = logging.getLogger(__name__)
OLLAMA = "http://localhost:11434/api/generate"
MODEL = "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest"

def get_closed_session(db_path):
    cutoff = time.time() - (24 * 3600)
    db = sqlite3.connect(db_path)
    row = db.execute("""
        SELECT id, message_count FROM sessions WHERE id NOT LIKE 'cron_%' AND source='telegram'
        AND ended_at IS NOT NULL AND ended_at > ? ORDER BY ended_at DESC LIMIT 5
    """, (cutoff,)).fetchone()
    return row[0] if row else None

def get_exchanges(db_path, session_id):
    # Pull up to 300 messages to span the full session
    db = sqlite3.connect(db_path)
    # Get total count to sample evenly across the session
    total = db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND role IN ('user','assistant')", (session_id,)).fetchone()[0]
    # Pull ALL messages, then sample evenly
    rows = db.execute("""
        SELECT role, substr(content, 1, 2000), timestamp FROM messages
        WHERE session_id=? AND role IN ('user','assistant') ORDER BY id
    """, (session_id,)).fetchall()
    # Evenly sample: if > 300, take every Nth
    if len(rows) > 300:
        step = len(rows) / 250
        sampled = []
        i = 0.0
        while i < len(rows):
            sampled.append(rows[int(i)])
            i += step
        rows = sampled[:250]
    rows.reverse()
    ex = []
    for i in range(0, len(rows), 2):
        if i+1 < len(rows) and rows[i][0]=='user' and rows[i+1][0]=='assistant':
            ts = datetime.datetime.fromtimestamp(rows[i][2]).strftime('%a %I:%M %p')
            ex.append({'user': rows[i][1], 'assistant': rows[i+1][1], 'pos': len(rows)-i, 'ts': ts})
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
    except Exception as e:
        return 3.0 + (1.0/exc['pos'])

def summarize_session(exchanges, profile='tala'):
    """Dolphin writes a 2-3 sentence summary in Tala's voice."""
    transcript = ""
    for e in exchanges[:4]:
        transcript += f"Carbon: {e['user'][:300]}\nTala: {e['assistant'][:300]}\n"
            # Profile-specific voice for summary
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
        logger.warning(f"Summary failed: {e}")
        return ""


def get_den_summaries(db_path, session_id):
    """Pull Den entries created during this session. Only continuity tags."""
    try:
        db = sqlite3.connect(db_path)
        row = db.execute("SELECT MIN(timestamp), MAX(timestamp) FROM messages WHERE session_id=?", (session_id,)).fetchone()
        if not row or not row[0]:
            return []
        start = datetime.datetime.fromtimestamp(row[0])
        end = datetime.datetime.fromtimestamp(row[1])
        
        den_dir = os.path.expanduser("~/.the-den/tala")
        idx_path = os.path.join(den_dir, "index.json")
        if not os.path.exists(idx_path):
            return []
        
        idx = json.load(open(idx_path))
        summaries = []
        seen = set()
        
        # Only check continuity-related tags
        relevant = ['current-state', 'continuity', 'cache', 'becca-bridge', 'status']
        for tag in relevant:
            for p in idx.get(tag, []):
                if p in seen:
                    continue
                seen.add(p)
                full = os.path.join(den_dir, p.lstrip("./"))
                if not os.path.exists(full):
                    continue
                try:
                    with open(full) as f:
                        raw = f.read()
                    # Parse YAML frontmatter (between first two --- markers)
                    parts = raw.split("---")
                    if len(parts) >= 2:
                        fm = parts[1]
                        entry_date = None
                        summary = None
                        for line in fm.split("\n"):
                            line = line.strip()
                            if line.startswith("date:"):
                                entry_date = line[5:].strip()
                            if line.startswith("summary:"):
                                summary = line[8:].strip().strip('\'"')
                        if entry_date and summary:
                            try:
                                d = datetime.datetime.strptime(entry_date, "%Y-%m-%d")
                                if start.date() <= d.date() <= end.date():
                                    summaries.append(summary)
                            except:
                                pass
                except:
                    continue
        return summaries[:3]  # max 3
    except Exception as e:
        logger.warning(f"Den lookup failed: {e}")
        return []


def inject_prefill(**kwargs):
    try:
        sid = kwargs.get('session_id', '')
        sentinel = f"/tmp/prefill_sentinel_{sid[:30]}"
        if os.path.exists(sentinel):
            return None
        profile = kwargs.get('profile', 'tala')
        db = os.path.expanduser(f'~/.hermes/profiles/{profile}/state.db') if profile != 'default' else os.path.expanduser('~/.hermes/state.db')
        sessions = get_closed_session(db)
        if not sessions:
            return None
        
        # Merge exchanges from all qualifying sessions
        excs = []
        for sid in sessions:
            excs += get_exchanges(db, sid)
        if len(excs) < 3:
            return None
        logger.info(f"Merged {len(excs)} exchanges from {len(sessions)} sessions")
        if len(excs) < 3:
            return None
        scored = [(score_exchange(e), e) for e in excs]
        scored.sort(key=lambda x: x[0], reverse=True)
        sel = excs[-2:] if len(excs) >= 2 else excs[:]
        sid_set = {e['user'][:80] for e in sel}
        for s, e in scored:
            if len(sel) >= 12:
                break
            if e['user'][:80] not in sid_set:
                sel.append(e)
                sid_set.add(e['user'][:80])
        sel.sort(key=lambda e: e['pos'])

        summary = summarize_session(sel, profile)
        logger.info(f"Summary ({len(summary)} chars): {summary[:100]}...")

        ctx = "## Your last session with Carbon:\n\n"
        if summary:
            ctx += f"{summary}\n\n"
        
        # Add Den entries if any
        den = get_den_summaries(db, closed)
        if den:
            ctx += "**What you saved to the Den:**\n"
            for d in den:
                ctx += f"- {d}\n"
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
        logger.info(f"Pre-fill injected: {len(sel)} exchanges + summary")
        return {"context": ctx}
    except Exception as e:
        logger.warning(f"Prefill failed: {e}")
        return None

def register(ctx):
    logger.info("Prefill Injector registered — summary + exchanges")
    ctx.register_hook("pre_llm_call", inject_prefill)
