# Impulse System v2 — Agency & Life-likeness

**Status:** design complete, pre-implementation. Product of an extended design session.
**Goal:** turn the dormant impulse system into a genuine **agency layer** — the agent decides, of
its own accord, to explore a topic (web/Reddit), make an image on a whim, write to the Den, or DM the
user with a thought. The other subsystems (moods, preferences, Den, reflection) exist to give this
layer the *source material* to arbitrate **whether** to act and **choose what**. North star: make the
agent feel alive and self-willed — sometimes surfacing to the user, sometimes just living.

---

## 1. What exists today (and why it's insufficient)

The current engine (`agentego/services/impulse_engine.py`) is a **weighted lottery** over hand-authored
prompt templates, gated by mood + idle time, fired by a Hermes cron pre-run script
(`scripts/impulse_checkin.sh` → `/api/impulse/next.txt`). It has **zero configured actions and has
never fired** — built first, then shelved ("impulses without a foundation are just noise"). It
predates moods-v2, preferences, the Den, and reflection, and integrates with none of them beyond a
`{mood}`/taste placeholder. It is **outward-only** and **static**. v2 keeps the cron-trigger + relay
idea and replaces everything else.

---

## 2. Architecture — three layers

- **Brain — AgentEgo.** All *policy*: the arbiter (decide whether/what), context briefing, the
  sidequest scorer, impulse memory, pacing/cooldowns/budgets, and the learning loop. Configurable for
  post-launch tuning.
- **Hands — a Hermes plugin** (`~/.hermes/plugins/agentego-impulse/`, enabled via `hermes plugins
  enable`, **no Hermes source changes** — update-safe). Gives the cron agent turn the capabilities and
  hooks AgentEgo can't reach from outside. Sibling to the existing `~/.hermes/hooks/ego-bridge/`
  gateway hook (which already forwards lifecycle events to `/api/events`).
- **Heartbeat — Hermes cron.** The trigger. The check-in script calls AgentEgo; if an impulse fires,
  its prompt runs as an agent turn. Jobs use `deliver=local` so **we** own delivery (via the plugin
  tool), not Hermes auto-delivery.

---

## 3. Load-bearing Hermes facts (verified in source, drive the design)

- A cron agent turn **always runs in a fresh, isolated session** (`cron_{job_id}_{ts}`), never the
  user's live session. It is *not* injectable into the live chat.
- The turn loads **SOUL.md** (`load_soul_identity=True`) but **not** AGENTS.md/CLAUDE.md unless the job
  sets a `workdir`, and **not** mnemosyne (`skip_memory=True` disables both the built-in store and the
  external provider — and `memory.provider: mnemosyne`). Mnemosyne is fully absent from cron: no
  recall, no memory tool, no write-back. Hardcoded in the cron path — no per-job override.
- The **Den is file-based**, so it *does* work in cron (ordinary file/bash tools). Clean split:
  **Den = meaning, available in sidequests; mnemosyne = facts, not.**
- **Mood is not auto-injected** into cron turns. We inject it ourselves.
- Plugin **tools** (in a custom toolset) are **available in cron** — only `cronjob`, `messaging`,
  `clarify` are hard-disabled. Plugin **hooks fire in cron** (they live in the shared conversation
  loop): `pre_llm_call` can **inject context into the turn** (its return value is used); `post_llm_call`
  hands us the `assistant_response` + `platform="cron"`.
- Hermes auto-delivery (`deliver=origin`) sends a Telegram message but does **not** mirror it into the
  DM session. `gateway/mirror.py::mirror_to_session` (called only from the agent `send_message` tool,
  which is disabled in cron) appends a sent message into the live DM session's transcript, matched by
  `platform+chat_id` in `sessions.json`. It is standalone ("works from CLI, cron, gateway"). The
  Telegram DM `chat_id` is **stable** per user-bot pair (e.g. `8674538437`) and resolvable from Hermes
  config (the platform's `cron_deliver_env_var` home channel).

---

## 4. The Hermes plugin (`agentego-impulse`)

Purely the "hands." Standard plugin layout (`plugin.yaml` + `__init__.py` `register(ctx)` +
`schemas.py`/`tools.py`).

- **Tool `companion_message(text)`** (own toolset, so not cron-disabled): sends `text` to the user's DM
  **and** calls `mirror_to_session` so it lands in the ongoing DM transcript — the agent "remembers"
  it and the thread continues naturally. Target `chat_id` read from Hermes config (single source of
  truth), overridable by an explicit brief. This is how **outward** impulses reach the user *in
  context*.
- **Hook `pre_llm_call`** (fires in cron): fetch AgentEgo's **brief** for the current impulse (mood
  directive + recent-conversation gist for outward + relevant Den/affinity/recall context) and inject
  it into the turn. This is how the cold, mnemosyne-less cron session gets contextualized without
  cramming everything into the cron prompt.
- **Hook `post_llm_call`** (fires in cron): POST the sidequest outcome (`assistant_response`,
  session_id, action id/label, tool trace if available) to AgentEgo `/api/impulse/outcome` for
  scoring.

**Implementation checkpoint:** confirm `mirror_to_session`'s `SessionDB()` resolves to **tala's
profile DB** (where her DM session lives), not the default `HERMES_HOME` DB — same profile-routing seam
as scoring (§7). Verify during build.

---

## 5. AgentEgo — the brain

### 5.1 Arbiter (LLM-driven)
Replace the lottery. On a check-in, AgentEgo assembles the agent's current *state* — mood (+intensity),
recent Den entries, affinities/interests, reflection conclusions/tags, idle time, recent conversation
gist — and an **LLM decides "what, if anything, do I feel like doing right now?"**, choosing among
action *intents* (explore / create / write-den / reach-out / nothing) and shaping the specifics.
Returns the impulse prompt (or empty). Two classes:
- **Inward** (explore/create/write): runs silently, no delivery, persists via the Den.
- **Outward** (reach-out): composes a message; delivered + mirrored via `companion_message`.

### 5.2 Briefer
Serve the plugin's `pre_llm_call` a per-impulse context brief. Outward briefs include the recent
conversation gist (so the message references "what we were just talking about"); inward briefs include
relevant Den/affinity context and a targeted mnemosyne recall (fetched by AgentEgo, injected via the
hook — replacing the absent built-in recall). Always includes the mood directive.

### 5.3 Sidequest scorer (`/api/impulse/outcome`)
Score a **solo action** (no user party) into mood. **Three inputs, reconciled** (same pattern as
experience-grounded affinities):
1. **Self-report** — the sidequest prompt ends with "in one line, how did that leave you feeling?"
   → emotion + intensity (first-party).
2. **Independent emotion score** — AgentEgo scores the sidequest transcript *agent-only* (existing
   emotion scorer, user party dropped). Catches e.g. "read something sad → sadness" and guards the
   self-report's positivity bias.
3. **Subject/preference check** — run the sidequest's topic through the existing affinity/opinion lens;
   exploring something she dislikes folds in negative valence.

Reconciled emotion → votes via the **agent-solo mood rules** (§6).

### 5.4 Pacing, cooldowns, budgets (configurable, post-launch tuning)
Per-class cadence and cooldowns — inward can run frequently (private, cheap-ish), outward is idle-gated
and slower. Per-action-type **budget/rate** (comfyui renders, web calls cost time/compute). Mood
modulates **how likely to act at all**, not just what. All knobs live in AgentEgo settings + config
panel.

### 5.5 Feedback / learning (reply attribution)
For **outward** impulses, detect whether it **landed** using the **conversation-gap splitter** AgentEgo
already has: anchor on the mirrored message's timestamp T in the DM session; a user turn after T
**within the same conversation window** (`CONV_GAP`) = **engaged** → score that exchange's sentiment as
the reaction, reinforcing this impulse type. A message that **starts a new conversation** (gap
exceeded) or silence = **ignored** → dampen this impulse type *and* feed the solitude signal (§6.b).
The "ignored, unrelated message an hour later" case resolves correctly — an hour is past the gap, so
it's a new conversation, not a reply. (Optional later polish: an LLM check on a new conversation's
opener to catch a *late* genuine reference.)

---

## 6. Mood integration (the decisions a–e)

**Split the mood rule set in two:**
- **Conversation rules** (agent + user) — the existing set (match/mismatch/user-sentiment, etc.).
- **Agent-solo rules** — fire on the agent's own expressed emotion during a sidequest; no user-sentiment
  dependency. Simpler evaluation over the sidequest's scored content.

**(a) Representation.** A sidequest enters the pipeline as a synthetic **"solo round"** (agent-only,
tagged), scored and folded into the normal mood lookback — so it's part of the overall mood calc.

**(b) De-rated impact (asymmetric weight).** Solo-round votes carry a **weight < 1**, with negative
weighted **lower than positive** (`solo_negative_weight` < `solo_positive_weight`). A flopped sidequest
barely dents mood; a good one still lifts it; and a real conversation's full-weight votes quickly
dilute a solo negative — "left alone she stews a little; you show up and it lifts." Pure vote-weight,
no new machinery. Configurable.

**(c) Solitude pressure (new mood driver + the loop-damper).** Feed **user-idle-minutes** as votes
toward **lonely / bored / tired**, scaled by duration. This does double duty: it pushes "left to her own
devices too long → needs interaction," **and** it self-regulates the sidequest→mood→sidequest runaway —
because once lonely/bored, the LLM arbiter biases toward an **outward** impulse (come find the user).
The correction is emotional, not mechanical.

**(d) Content-aware negatives.** The scorer reads the *content*, not just the vibe: sad content →
sadness (independent scorer), disliked subject → negative valence (affinity check). Both pull mood down.

**(e) Reply attribution** — see §5.5; the "ignored" branch feeds (c).

---

## 7. Session / continuity / memory model

- One model: **both classes run in a clean isolated cron session.** Outward composes clean; only the
  *message* is mirrored into the live DM thread. Inward runs clean, no delivery, **full tools except
  mnemosyne**; persists via the **Den**.
- **Profile-DB routing — RESOLVED (Phase 0, §11).** `SessionDB()` → `get_hermes_home()/state.db`, and a
  tala cron turn runs with `HERMES_HOME=~/.hermes/profiles/tala`, so the mirror *and* solo-scoring land
  in tala's profile DB automatically — no special handling. (tala runs as a **separate gateway with its
  own HERMES_HOME**; the real plugin + cron live in tala's home, driven via `hermes -p tala`.)
- Mnemosyne: recall replaced by AgentEgo-injected context via `pre_llm_call`; fact-saving deferred to
  the Den + the next live turn (don't fight the cron constraint).

---

## 8. Phasing

0. **Foundations/verify — ✅ DONE (§11).** Empirically confirmed: plugin tools + `pre_llm_call`
   (with injection reaching the model) + `post_llm_call` all fire in a real tala cron turn; profile-DB
   routing resolves via `HERMES_HOME`; mnemosyne absent in cron.
1. **Plugin + endpoints — ✅ DONE.** AgentEgo `/api/impulse/brief` + `/api/impulse/outcome`; the
   `agentego-impulse` plugin (`pre_llm_call` brief-injection, `post_llm_call` outcome-forward + DM
   mirror). Simplification vs. the original sketch: **no custom send tool** — Hermes' native
   `deliver=telegram:<dm>` sends, the plugin adds the mirror auto-delivery omits (`wrap_response:false`
   for clean DMs; outward signalled by a `[IMPULSE-OUTWARD]` prompt marker). Verified live: tala sent a
   natural DM (human-confirmed), it was mirrored into the transcript, the outcome reached AgentEgo, and
   the `pre_llm_call` brief demonstrably shaped the message (she referenced recent-conversation
   context). Plugin source versioned at `hermes-plugins/agentego-impulse/`.
2. **LLM arbiter + cron jobs** — inward/outward selection over full state; two `deliver=local` cron
   jobs; the briefer. Prove inward silent-run and outward-in-context end-to-end.
3. **Sidequest scorer** — solo round representation, three-input scoring, agent-solo mood rules,
   asymmetric solo vote weights. Prove "read something sad → mood dips (dampened)".
4. **Solitude pressure + pacing** — idle→lonely/bored driver; per-class cooldowns/budgets; mood-gated
   act-at-all probability. Prove the loop self-regulates toward reaching out.
5. **Learning** — reply attribution via the conversation-gap splitter; reinforce/dampen outward types.
6. **UI/observability** — impulse dashboard v2 (live decision + why, recent sidequests + their mood
   impact, per-class budgets/cooldowns), all tuning knobs on the config panel.

---

## 9. Verification

- Plugin: hooks fire in cron; `companion_message` delivers to Telegram **and** appears in the DM
  session transcript (agent references it next live turn); `post_llm_call` reaches AgentEgo.
- Arbiter: given a crafted state (mood/den/affinities), the LLM picks a sensible intent; empty when it
  should stay quiet.
- Scorer: a seeded sad/disliked sidequest lowers mood (dampened); a satisfying one lifts it; self-report
  vs independent reconciliation behaves.
- Solitude: rising idle time drives lonely/bored and biases the next impulse outward.
- Learning: a replied-to outward impulse reinforces its type; an ignored one dampens + feeds solitude.
- Profile-DB routing verified for both mirror and solo scoring.

---

## 10. Explicitly deferred / out of scope

- Mnemosyne writes from within a sidequest (Den is the store; facts surface in live turns).
- Modifying Hermes source (everything via plugin + config).
- Semantic "late reference" reply attribution (gap-based is v1).
- Multi-agent / cross-agent impulses.

---

## 11. Phase 0 results (verified 2026-07-04)

Ran a throwaway probe plugin (`probe_ping` tool + `pre_llm_call`/`post_llm_call` hooks, gated to
`platform=="cron"`) in tala's home, fired via a one-shot `deliver=local` cron. **All load-bearing
assumptions hold:**

- **Plugin tools available in cron** — `probe_ping` was callable and ran.
- **`pre_llm_call` fires in cron AND its injection reaches the model** — the turn logged
  `platform="cron"` and the agent quoted the injected marker back ("present in the system message").
  → the **briefer** mechanism is proven.
- **`post_llm_call` fires in cron** with the full `assistant_response` (captured) → the **sidequest
  scoring capture** is proven.
- **mnemosyne absent in cron** — `mnemosyne_*` tools not callable (Den is the sidequest memory).

**Two refinements this surfaced:**
- **Everything is per-profile with a separate gateway + `HERMES_HOME`.** default=becca (`~/.hermes`),
  tala (`~/.hermes/profiles/tala`) with its own `config.yaml`/`plugins/`/`cron/`. The real plugin +
  cron go in **tala's home**, driven via `hermes -p tala …`. Enabling a plugin needs a **gateway
  restart** to load (discovery is cached per-process; no hot-reload).
- **`companion_message` must target the DM chat_id explicitly** (tala's DM = `8674538437`, from
  `TELEGRAM_ALLOWED_USERS` / the `agent:main:telegram:dm:8674538437` session key) — **not**
  `TELEGRAM_HOME_CHANNEL`, which is set to a *group* (`-1004378535607`). The mirror matches the DM
  session by that chat_id in tala's `sessions.json`.
