#!/usr/bin/env python3
"""
Garden Game State — load/save helpers
Tracks: active cycle, plant threads, scores, used sessions
"""
import json, os, sys

STATE_PATH = os.path.expanduser("~/.hermes/state/garden.json")
SCHEMA_VERSION = 1

def load():
    if not os.path.exists(STATE_PATH):
        return {"schema_version": SCHEMA_VERSION, "active_cycle": None, "cycles": [], "last_used_sessions": []}
    with open(STATE_PATH) as f:
        return json.load(f)

def save(state):
    state["schema_version"] = SCHEMA_VERSION
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def start_cycle(channel_id):
    state = load()
    import time
    state["active_cycle"] = {
        "channel_id": channel_id,
        "started_at": time.time(),
        "day": 1,
        "phase": "planting",
        "plants": [],
        "pruned": []
    }
    save(state)
    return state["active_cycle"]

def get_active_cycle():
    state = load()
    return state.get("active_cycle")

def add_plant(thread_id, thread_url, title, source_session_id, day_planted):
    state = load()
    cycle = state["active_cycle"]
    if not cycle:
        return None
    import time
    plant = {
        "thread_id": thread_id,
        "thread_url": thread_url,
        "title": title,
        "source_session_id": source_session_id,
        "day_planted": day_planted,
        "score": 50,
        "last_tended_day": day_planted,
        "messages_from_player": 0
    }
    cycle["plants"].append(plant)
    state["last_used_sessions"].append(source_session_id)
    save(state)
    return plant

def record_player_message(thread_id, day):
    state = load()
    cycle = state["active_cycle"]
    if not cycle:
        return None
    for plant in cycle["plants"]:
        if plant["thread_id"] == thread_id:
            plant["messages_from_player"] += 1
            plant["score"] = min(100, plant["score"] + 5)
            plant["last_tended_day"] = day
            save(state)
            return plant
    return None

def prune_lowest():
    state = load()
    cycle = state["active_cycle"]
    if not cycle or not cycle["plants"]:
        return None
    lowest = min(cycle["plants"], key=lambda p: p["score"])
    cycle["plants"].remove(lowest)
    cycle["pruned"].append(lowest)
    save(state)
    return lowest

def advance_day():
    state = load()
    cycle = state["active_cycle"]
    if not cycle:
        return None
    cycle["day"] += 1
    if cycle["day"] > 14 and cycle["phase"] == "planting":
        cycle["phase"] = "tend_prune"
    if cycle["day"] > 28:
        cycle["phase"] = "complete"
    save(state)
    return cycle

def end_cycle():
    state = load()
    completed = state["active_cycle"]
    if completed:
        state["cycles"].append(completed)
        state["active_cycle"] = None
        save(state)
    return completed

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "show":
            state = load()
            print(json.dumps(state, indent=2))
