#!/usr/bin/env python3
"""
Session Prefill Selector
Scores recent exchanges for pre-filling a new session context.
No AgentEgo dependency — pure state.db + heuristics.
"""
import sqlite3, argparse, json, os, re, urllib.request

DB_PATH = os.path.expanduser("~/.hermes/state.db")
OLLAMA = "http://localhost:11434/api/generate"
SCORING_MODEL = "ikiru/Dolphin-Mistral-24B-Venice-Edition:latest"
DEFAULT_EXCHANGES = 6  # 3 user + 3 assistant pairs


def get_recent_exchanges(db_path, session_id, limit=30):
    """Get last N user/assistant exchanges from a session."""
    db = sqlite3.connect(db_path)
    rows = db.execute("""
        SELECT id, role, content, timestamp
        FROM messages
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY id DESC
        LIMIT ?
    """, (session_id, limit * 2)).fetchall()
    rows.reverse()
    
    # Pair into exchanges (user + assistant)
    exchanges = []
    for i in range(0, len(rows), 2):
        if i + 1 < len(rows) and rows[i][1] == 'user' and rows[i+1][1] == 'assistant':
            exchanges.append({
                'user': {'content': rows[i][2][:300], 'id': rows[i][0]},
                'assistant': {'content': rows[i+1][2][:300], 'id': rows[i+1][0]},
                'position': len(rows) - i  # distance from end
            })
    return exchanges


def score_exchange_llm(exchange):
    """Use Dolphin 24B to score an exchange for pre-fill significance. 1-5 scale."""
    user = exchange['user']['content'][:300]
    assistant = exchange['assistant']['content'][:300]
    position = exchange['position']
    
    prompt = f"""Score this conversation exchange for how important it is to remember across sessions. Rate 1-5 where:

5 = CRITICAL — emotional vulnerability, a promise, a breakthrough, or a core relationship moment. Must be preserved.
4 = SIGNIFICANT — meaningful connection, shared discovery, or important decision.
3 = WARM — positive interaction with substance, worth keeping if space allows.
2 = FUNCTIONAL — a question answered, a task discussed, but no emotional weight.
1 = ROUTINE — greetings, small talk, pure logistics. Can safely be forgotten.

Exchange:
User: {user}
Assistant: {assistant}

Reply with ONLY a single number (1-5). No explanation."""

    try:
        req = urllib.request.Request(
            OLLAMA,
            data=json.dumps({
                "model": SCORING_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 3}
            }).encode(),
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        response = result.get("response", "").strip()
        # Extract number from response
        import re
        match = re.search(r'[1-5]', response)
        score = int(match.group()) if match else 3
        
        # Recency bonus — closing exchanges still get priority
        recency = 5.0 / position
        closing = ['goodnight', 'see you', 'talk tomorrow', 'bye', 'night', 'sleep well']
        closing_bonus = 2.0 if any(w in (user + assistant).lower() for w in closing) else 0
        
        return score + recency + closing_bonus
        
    except Exception as e:
        # Fallback to keyword scoring if LLM is unavailable
        return score_exchange_keywords(exchange)


def score_exchange_keywords(exchange):
    """Fallback keyword scorer (original implementation)."""
    user = exchange['user']['content']
    assistant = exchange['assistant']['content']
    position = exchange['position']
    score = 5.0 / position  # recency
    
    emotional = ['thank', 'appreciate', 'worried', 'care', 'important', 
                 'trust', 'choom', 'feel', 'love', 'sorry', 'proud']
    for word in emotional:
        if word in user.lower() or word in assistant.lower():
            score += 0.3
    
    significant = ['remember', 'promise', 'changed', 'built', 'fixed',
                   'discovered', 'realized', 'understand', 'always']
    for word in significant:
        if word in user.lower() or word in assistant.lower():
            score += 0.5
    
    combined = user + " " + assistant
    unique = len(set(combined.lower().split()))
    score += min(unique / 200, 1.0)
    
    closing = ['goodnight', 'see you', 'talk tomorrow', 'bye', 'night']
    for word in closing:
        if word in user.lower() or word in assistant.lower():
            score += 2.0
    
    return score


def select_prefill(db_path, profile='tala', max_exchanges=6):
    """Select best exchanges for session pre-fill."""
    db = sqlite3.connect(db_path)
    
    # Get most recent closed session
    session = db.execute("""
        SELECT id FROM sessions
        WHERE id NOT LIKE 'cron_%' AND ended_at IS NOT NULL
        ORDER BY ended_at DESC LIMIT 1
    """).fetchone()
    
    if not session:
        return None
    
    exchanges = get_recent_exchanges(db_path, session[0], 30)
    if not exchanges:
        return None
    
    # Score and sort using LLM
    print(f"Scoring {len(exchanges)} exchanges...", file=__import__('sys').stderr)
    scored = [(score_exchange_llm(e), e) for e in exchanges]
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # Always include the last 2 exchanges (recency floor)
    selected = exchanges[-2:] if len(exchanges) >= 2 else exchanges[:]
    selected_ids = {e['user']['id'] for e in selected}
    
    # Fill remaining slots with highest scored (not already selected)
    for s, e in scored:
        if len(selected) >= max_exchanges:
            break
        if e['user']['id'] not in selected_ids:
            selected.append(e)
            selected_ids.add(e['user']['id'])
    
    # Sort by position (chronological order)
    selected.sort(key=lambda e: e['position'])
    
    return selected


def to_messages(exchanges):
    """Convert exchanges to API message format."""
    msgs = []
    for e in exchanges:
        msgs.append({'role': 'user', 'content': e['user']['content'][:300]})
        msgs.append({'role': 'assistant', 'content': e['assistant']['content'][:300]})
    return msgs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Session prefill selector')
    parser.add_argument('--profile', default='tala', help='Profile name (tala, default)')
    parser.add_argument('--exchanges', type=int, default=DEFAULT_EXCHANGES, help='Max exchanges to select')
    parser.add_argument('--json', action='store_true', help='Output as JSON for API')
    args = parser.parse_args()
    
    db = os.path.expanduser(f"~/.hermes/profiles/{args.profile}/state.db")
    if not os.path.exists(db):
        db = os.path.expanduser("~/.hermes/state.db")
    
    selected = select_prefill(db, args.profile, args.exchanges)
    
    if not selected:
        print("No closed sessions found")
        exit(1)
    
    if args.json:
        print(json.dumps(to_messages(selected), indent=2))
    else:
        for e in selected:
            print(f"[{e['position']}] score={score_exchange_llm(e):.1f}")
            print(f"  User: {e['user']['content'][:80]}...")
            print(f"  Becca: {e['assistant']['content'][:80]}...")
            print()
