# Mood Scoring Refactor — LLM backbone + a small, legible per-profile corrective layer

**Status:** design. Prompted by the mood system becoming too complex to comprehend/tune. Product of an
audit + a data-driven replay analysis (2026-07-05).

## Context & motivation

The mood engine accreted over time. The oldest layer is a hand-rolled **rule engine** — ~57 rules per
profile, each matching emotions with discrete `x in N of last M rounds` logic (`lookback` / `min_count`
/ `streak` / `cumulative` / top-3 cliff). A **per-round LLM mood scorer** was added later and now runs
in parallel, predicting which moods each round evokes. On top sits the **shaping layer** (cascade,
tenure-bias, cooldown, hysteresis — recently fixed, out of scope here).

The rule engine is now (a) too large to hold in one's head, (b) brittle (a single off/empty round flips
a 2-of-3 window and silently kills a rule), and (c) largely **redundant** with the LLM scorer.

### Evidence (replay of the *current* config against historical raw per-round enrichment)

Method note: we do **not** trust stored historical mood *decisions* (each ran against a different config
version). We replay the **current** rules + LLM-vote logic against each round's stored **raw** `sentiment`
+ `mood_scores` (the stable substrate), sliding a 20-round window. `prev_mood` rules excluded (unreplayable).

| metric | tala (romantic) | becca (work/focused) |
|---|---|---|
| LLM-only winner == combined winner | **92%** | **97%** |
| rules flipped the outcome | 7% | 2% |
| rule share of vote mass | 30% | 25% |
| dead rules (never fired) | 13/57 | 16/56 |

**Conclusions:**
1. **The LLM votes are the backbone** — right ~92–97% alone, across two very different personas. Robust.
2. **Rules are a thin minority corrective**, not the engine — and mostly *redundant reinforcement*:
   "keeper" (decided a winning mood) ≠ "corrective" (changed the outcome). becca's top keeper *"locked
   in"* decided 120 windows but flipped only ~4 — it just echoes the LLM's Focused call.
3. **The genuine corrections are per-profile and tiny.** tala's flips are all *affectionate→flirty* (the
   warmth boundary); becca's are *focused→playful*. The rule sprawl exists because **two personas' needs
   pooled into one shared set** — any one persona uses ~3–6 of them.

## The new model

Replace the vote-**generation** layer; keep the vote-**shaping** layer (cascade/bias/cooldown/hysteresis)
untouched.

```
per-round LLM mood scores ──► LLM backbone votes ─┐
                                                  ├─► vote_map ─► [cascade → tenure-bias →
per-profile corrections (few) ─► corrective votes ┘                cooldown → hysteresis] ─► mood
```

- **Backbone:** the per-round mood prediction from the **local scorer** (Ollama `Dolphin-Mistral`, via the
  sentiment worker) — the same `mood_scores` already produced today. **Decided: continuous** — a
  recency-weighted sum of the raw 0–10 scores, replacing the discrete `score≥T → +1` voting (drops that
  threshold knob; smoother, no cliff).
- **Corrective layer:** a *small* per-profile set of **continuous emotion→mood affinities** that add
  (or subtract) support where the LLM systematically misses. No `lookback`/`min_count`/`streak`/cliffs —
  one global recency decay + per-correction weights. Bounded strength so a correction **nudges** and only
  flips a *near-tie*, never overrides a confident LLM call.

### LLM usage (important — keep the panel honest)
The **only** LLM in the scoring path is the **local scorer** producing the backbone `mood_scores`.
Everything else is deterministic: the corrective math, and — see the UI below — the **Gaps** and the
**Why** narrative are computed from the structured decision state + stored round enrichment (templated
strings), **no LLM call**. This is deliberate: a template is truthful *by construction* (it can't drift
from the numbers — the phantom-`−4` lesson), it's free, and it works offline. An optional *prettier*
prose narrative could later sit on the **remote** model (deepseek), cached per mood-change, but never as
the source of truth.

### Correction data model

Stored per profile (JSON setting or a `mood_corrections` table). Each correction is one legible unit:

```jsonc
{
  "id": "flirty-desire",
  "target_mood": "flirty",
  "agent_emotions": { "desire": 1.0, "arousal": 1.0, "lust": 0.8, "yearning": 0.5 }, // continuous affinity
  "strength": 0.6,          // 0..1 overall multiplier (bounded)
  "mutual": false,          // optional: also require/boost the USER scoring these (relational signal)
  "mode": [],               // optional categorical trigger (conversation mode)
  "topic_contains": [],     // optional topical trigger
  "note": "The LLM reads warmth as affectionate; nudge toward flirty when desire/heat is present.",
  "enabled": true
}
```

Contribution = `strength × Σ_rounds( recency_weight(round) × Σ_e( weight[e] × agent_score[round][e] ) )`,
with one global `mood_recency_halflife` (rounds). `mutual` adds a bounded bonus when the user party also
scores the emotions (covers the surviving `sentiment_match` value, e.g. tala's "both feel affection", 37×).

### Deriving the initial corrections (migration — don't hand-convert 57 rules)

Run the **flip analysis** per profile → the handful of rules that genuinely *flipped* the LLM become the
seed corrections; their emotion sets become the affinity weights; the flip direction is the target mood.
Everything else (dead + pure-reinforcement + never-fired) is dropped. Expected seed size: **~3–6 for tala**
(affection→affectionate, desire→flirty, mutual-affection bonus), **~0–2 for becca** (the LLM already nails
Focused; maybe a focused↔playful nudge).

## The corrective-layer UI (the comprehension win)

The whole point: the corrective layer is small enough to fit on **one screen as readable cards**, each
showing its **live effect** — versus scrolling 57 opaque rules. Replaces the current mood-debug panel.

```
┌─ Mood: tala ───────────────────────────── now: CREATIVE (▲ +3 over Affectionate) ─┐
│                                                                                    │
│  WHY  "She's been on creative work ~10 rounds; the LLM keeps scoring it high.     │
│        Affectionate is close behind — your flirty nudge isn't firing (no heat)."  │  ← templated, no LLM
│                                                                                    │
│  LLM READ (the backbone)              CORRECTIONS (yours)                          │
│   Creative   ████████████ 12           ● Flirty  ← desire/arousal/heat            │
│   Affection  ██████ 6                     strength ●●●○○   NOW: dormant (0.0)      │
│   Focused    █████ 5                    ● Affectionate ← affection/tenderness/love │
│   Curious    ████ 4                       strength ●●○○○   NOW: +2.1  ✓ firing     │
│   …                                     ● Mutual-affection bonus  NOW: +0.5 ✓      │
│                                         [+ add correction]                         │
│                                                                                    │
│  RESULT  Creative 12  ·  Affectionate 6+2.1=8.1  ·  gap 3.9  ·  no flip            │
│  GAPS    ⚠ "Loneliness scored in 3 recent rounds; nothing corrects for it. Add?"  │
└────────────────────────────────────────────────────────────────────────────────── ┘
```

Editing a correction (click a card): pick **target mood**, add **emotions** as weighted tags (from the
taxonomy), set **strength** (slider), optional **mutual / mode / topic** modifiers, a **note**. Live
readout updates as you edit ("would have fired +1.4 over the last 20 rounds"). Three panels total:

1. **Now / Why** — current mood, margin to runner-up, one-line narrative **templated from the decision
   state (no LLM call)** — truthful by construction.
2. **LLM read vs. your corrections** — the backbone ranked, then each correction with a *live* contribution
   (firing / dormant / by how much). This is the "vote journey" made trivial because there are ~5 items,
   not 57.
3. **Gaps** — the flip/coverage analysis running continuously, **pure computation (no LLM)**: surfaces
   emotions/moods appearing in recent rounds that nothing corrects for ("add a correction?"), and flags
   corrections that never fire (dead) or fight each other. Turns tuning into *reacting to surfaced gaps*
   instead of auditing a wall.

## Staged rollout (de-risk — never rip-and-replace)

Setting `mood_scoring_mode: legacy | shadow | corrective`.
1. **Shadow** — compute LLM+corrections alongside the live legacy rules; **legacy still drives** the mood.
   Log both winners each eval. Watch divergence over the soak; confirm divergences are improvements (the
   Gaps panel shows where/why).
2. **Cutover** — flip to `corrective`; LLM+corrections drive, legacy stays computable for side-by-side.
3. **Delete** — once trusted, remove the 57-rule engine + its params/UI.

## Decided
- **Continuous backbone** — recency-weighted raw 0–10 local-scorer mood scores; discrete `≥T` voting removed.
- **Gaps + Why are deterministic** (no LLM); the only LLM in the path is the local backbone scorer.
- **Ship this refactor to *production* (shadow → cutover → delete legacy) before resuming Phase 3.**
- **Storage: a `mood_corrections` table** (not a JSON blob) — the UI does per-row add/edit/delete/enable,
  so a table is the clean fit.
- **Correction strength is a bounded, config-scaled nudge.** A global `mood_correction_scale` (vote-equiv
  at `strength=1.0`, **default ~4**) caps how much any correction can add — enough to flip a genuine
  near-tie, never a confident LLM lead. Starting default aims for the observed ~7% (tala) / ~2% (becca)
  flip rate; **tuned during shadow**, not pre-guessed.
- **Recency: global `mood_recency_halflife`, default ~8 rounds** (last ~10 rounds dominate); tuned in shadow.

## Build-derived / deferred (not pre-decisions)
- **becca's launch set** — *output* of the flip-analysis seeding tool, not a decision (expected 0–2). Same
  tool produces tala's ~3–6.
- **Solo/impulse rounds feeding corrections** — a Phase-3 concern: they'll feed at a dampened recency
  multiplier (the solo-weight idea), settled when Phase 3 resumes; excluded from the backbone for now.

## Interaction with the impulse work (Phase 3)
This *simplifies* Phase 3. The parked "agent-solo mood rules / `sentiment_solo` rule type" concept goes
away — a sidequest's scored emotion just feeds the **same corrective/LLM pipeline at a dampened recency
weight** (the solo-weight idea survives as a per-round multiplier, not a new rule class). **Phase 3 is
parked until this refactor ships to production** (per decision above), so impulses plug into the
already-simplified model.

## Sequencing
1. Flip-analysis tool → seed corrections per profile (reuse the replay script already written).
2. Corrective scoring path + `mood_scoring_mode=shadow`; log new-vs-legacy.
3. Corrective-layer UI (3 panels above).
4. Soak in shadow → cutover → delete legacy.
5. Then resume Phase 3 on the simplified model.
