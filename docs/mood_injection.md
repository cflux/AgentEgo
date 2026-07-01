# Injecting the mood into the agent

AgentEgo computes the agent's mood; Hermes runs the agent. Like the impulse system, Hermes
**pulls** the mood from AgentEgo and adds it to the system prompt. AgentEgo never writes into
`SOUL.md` (that would clobber the persona and retrigger OCEAN re-abstraction) — the disposition is a
**separate block**.

## Endpoint

```
GET http://localhost:8765/api/mood/directive?profile=<name>
```

Returns a ready-to-inject, guardrailed disposition block (plain text), or **empty** when the
directive is disabled or there's no mood. It's stable between mood changes, so a per-turn fetch
naturally updates only when the mood actually changes. Example:

```
## Current disposition
You've recently been feeling **Flirty** (playful and flirty). Let it colour your tone, but follow
the user's lead and let it pass naturally — don't force it or escalate it.
```

The wording (and the anti-escalation guardrails) is editable at **/config → Agent disposition**.

## Wiring it into Hermes (per turn — recommended)

In the Hermes prompt assembly, append the fetched block **after** the SOUL.md persona:

```bash
MOOD="$(curl -s "http://localhost:8765/api/mood/directive?profile=tala")"
SYSTEM_PROMPT="${SOUL_MD}

${MOOD}"
```

A per-turn GET is the same cadence as reading SOUL.md; it's cheap and needs no writes.

## Alternative: file-based (only if Hermes can't fetch a URL)

Set **mood_directive_file** (e.g. `/hermes/profiles/tala/DISPOSITION.md`) on the config page.
AgentEgo writes the block there **on mood change**. Requirements:
- A **dedicated** file, never `SOUL.md`.
- A **writable** mount of that path into the AgentEgo container (compose currently mounts `/hermes`
  read-only, so this needs a rw mount of the profile dir or a shared path).
- Hermes prompt assembly must `include` that file after the persona.

## Why this won't get the agent stuck

Mood is measured partly from the agent's own output, so naive injection could self-reinforce. Guards:
- **Guardrail wording** in the block ("follow the user's lead, don't escalate, it will pass").
- **Homeostatic decay** (config → Fade): a mood that's held too long fades — *along with its cascade
  feeders* — and hands off to the next mood, with a cooldown so it can't bounce back.
- On-change-only updates mean the agent sees a stable disposition, not a per-turn-fluctuating one.

If it still sticks in practice, the Phase 2 lever is to weight the mood scoring toward the user's
affect over the agent's own expression (see the plan).
