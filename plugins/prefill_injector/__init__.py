"""Prefill Injector — injects session summary + key exchanges on session start."""
import logging, os, json, sqlite3, urllib.request, time, re, datetime, sys
sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
from platform_config import source_clause

logger = logging.getLogger(__name__)
OLLAMA = "http://localhost:11434/api/generate"
MODEL = "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest"

def get_closed_session(db_path):
    cutoff = time.time() - (24 * 3600)
    db = sqlite3.connect(db_path)
    row = db.execute(f"""
        SELECT id, message_count FROM sessions WHERE id NOT LIKE 'cron_%' AND {source_clause()}
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

def _get_channel_id(session_id):
    """Extract the Discord channel ID from session source_metadata.
    Returns parent_chat_id for threads (the actual channel), chat_id for
    direct channel messages, or None if undetermined."""
    try:
        db_path = os.path.expanduser('~/.hermes/state.db')
        db = sqlite3.connect(db_path)
        row = db.execute(
            "SELECT source_metadata FROM sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if not row or not row[0]:
            return None
        meta = json.loads(row[0])
        # For threads: parent_chat_id is the channel the thread lives in
        # For direct messages: chat_id is the channel
        return meta.get('parent_chat_id') or meta.get('chat_id')
    except Exception:
        return None


def _get_overnight_briefing():
    """Read overnight cron review outputs. Date-gated: once per day.
    Returns a compact briefing string or None if already consumed today."""
    today = datetime.date.today().isoformat()
    marker = f"/tmp/prefill_briefing_{today}"

    if os.path.exists(marker):
        return None  # already consumed today

    # Atomic claim — prevent races even though per-session sentinel covers it
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return None

    review_dir = os.path.expanduser("~/.hermes/cron/output")
    briefing_lines = []

    # Code Inspector Review (job 1dd2eb81edd8)
    ci_dir = os.path.join(review_dir, "1dd2eb81edd8")
    try:
        files = sorted(os.listdir(ci_dir), reverse=True)
        today_files = [f for f in files if f.startswith(today)]
        if today_files:
            # Check if the review found actual bugs (not all-clean)
            with open(os.path.join(ci_dir, today_files[0])) as f:
                content = f.read()
            briefing_lines.append("### 🔫 Code Inspector Review")
            # Extract bug count: "1 confirmed" or "1 real bug" patterns
            m = re.search(r'(\d+)\s+(?:confirmed|real)', content)
            if m and m.group(1) != '0':
                briefing_lines.append(f"- **{m.group(0)}** in overnight scan")
                # Try to extract the key finding
                fm = re.search(r'[*_]{2}🐛\s+(.+?)\s*[*_]{2}', content)
                if fm:
                    briefing_lines.append(f"- {fm.group(1)}")
    except Exception:
        pass

    # Spotter Review (job 8d485057ebf4)
    sp_dir = os.path.join(review_dir, "8d485057ebf4")
    try:
        files = sorted(os.listdir(sp_dir), reverse=True)
        today_files = [f for f in files if f.startswith(today)]
        if today_files:
            with open(os.path.join(sp_dir, today_files[0])) as f:
                content = f.read()
            briefing_lines.append("### 🛡️ Spotter Review")
            # Extract the summary line
            m = re.search(r'Summary\s*\n\s*(.+?)(?:\n|$)', content)
            if m:
                briefing_lines.append(f"- {m.group(1).strip()}")
            elif 'all clear' in content.lower() or '0 flags' in content.lower():
                briefing_lines.append("- All clear — nothing actionable")
    except Exception:
        pass

    if not briefing_lines:
        # No reviews found today — don't inject empty briefing,
        # but keep the marker so we don't keep checking
        return None

    return "## Overnight Briefing — " + today + "\n\n" + "\n".join(briefing_lines) + "\n"


def _build_session_prefill(kwargs, sentinel, log):
    """Build session prefill context — last session exchanges + Den entries.
    Returns context string or None if nothing to inject."""
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
    return ctx


def inject_prefill(**kwargs):
    log = logging.getLogger(__name__)
    log.info(f"Prefill hook fired — session={kwargs.get('session_id','?')[:20]}...")

    try:
        sid = kwargs.get('session_id', '')
        if not sid:
            return None

        context_parts = []

        # ═══ OVERNIGHT BRIEFING (date-gated, fires on ANY message, any channel) ═══
        briefing = _get_overnight_briefing()
        if briefing:
            log.info("Prefill: overnight briefing injected")
            context_parts.append(briefing)

        # ═══ SESSION PREFILL (sentinel-gated, #botwranger only) ═══
        debug_force = os.environ.get('PREFILL_DEBUG_FORCE', '') == '1'
        if debug_force:
            log.info("Prefill: force mode — bypassing gates")

        sentinel = f"/tmp/prefill_sentinel_{sid[:30]}"
        do_session_prefill = True  # assume yes, gates can flip this

        # Sentinel gate
        if not debug_force:
            try:
                fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except FileExistsError:
                log.info("Prefill: sentinel exists — skipping session prefill")
                do_session_prefill = False

        # Channel gate — only session prefill in #botwranger
        if do_session_prefill and not debug_force:
            platform = kwargs.get('platform', '')
            if platform == 'discord':
                channel_id = _get_channel_id(sid)
                BOTWRANGER = '1525303701718433903'
                if channel_id and channel_id != BOTWRANGER:
                    log.info(f"Prefill: not #botwranger (channel={channel_id}) — deferring session prefill")
                    try:
                        os.remove(sentinel)
                    except OSError:
                        pass
                    do_session_prefill = False
                elif not channel_id:
                    log.info("Prefill: couldn't determine channel — allowing session prefill (fallback)")

        if debug_force and do_session_prefill:
            for f in os.listdir('/tmp/'):
                if f.startswith('prefill_sentinel_'):
                    os.remove(os.path.join('/tmp/', f))

        # Run session prefill if gates all passed
        if do_session_prefill:
            session_ctx = _build_session_prefill(kwargs, sentinel, log)
            if session_ctx:
                context_parts.append(session_ctx)

        if context_parts:
            return {"context": "\n\n".join(context_parts)}
        return None

    except Exception as e:
        log = logging.getLogger(__name__)
        log.warning(f"Prefill failed: {e}", exc_info=True)
        return None

def register(ctx):
    logger.info("Prefill Injector registered — hooks: pre_llm_call")
    ctx.register_hook("pre_llm_call", inject_prefill)
