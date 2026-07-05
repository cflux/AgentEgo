# Mood rounds & feeder suppression — FIXED 2026-07-05

Two related mood-engine bugs found 2026-07-05 while investigating tala's sentiment rollups and the
"flirting mode but low flirty mood" observation. **Both fixed** (to establish a clean, correct mood
baseline before adding impulses). Kept here as a record + the tuning notes at the bottom.

## Fix 1 — round formation counts non-dialogue (tool/skill) messages

`split_into_rounds` (`agentego/services/conversations.py`) receives *all* messages — user, assistant,
tool-result, and empty pure-tool-call assistant messages. Only a user-speaking-after-the-agent starts a
new exchange, so tool/skill messages (image-gen: `terminal`, `execute_code`, `vision_analyze`,
`write_file`, `mnemosyne_remember`, and empty-content tool-call assistant turns) pile into the open round.

**Symptoms (today's 86-msg conv):** `msg_count` bloat (r8 = 28 msgs, 26 of them tool noise, scored off 1
sentence); a text-less agent round (r7, `agent=None`) when a turn was pure tool execution; thin sentiment
rollups.

**Impact:** mild. Fade is driven by *rounds* (= exchanges), and tool calls don't add rounds, so decay is
NOT accelerated. The only real cost is occasional null/thin rounds that tick tenure without re-supporting
the mood, plus misleading `msg_count`s. Cushioned by the LLM per-round mood-vote path.

**Fix:** build rounds from real dialogue only — `role in (user, assistant)` with non-empty content; drop
tool-result messages and empty pure-tool-call assistant messages before `split_into_rounds`. Re-shapes
existing rounds → wants a re-score. Won't change round *count* (exchanges unchanged), so fade timing is
unaffected; it just removes null/thin rounds and honest-izes `msg_count`.

## Fix 2 — incumbent anti-stuck bias suppresses its cascade feeders (CONFIRMED bug)

In `_transition_effective` (`agentego/services/mood_engine.py`), the negative incumbent bias is applied
not only to the incumbent but to **every mood in its reverse-cascade feeder chain**:

```python
if bias < 0 and cascade:
    decayed_chain = _reverse_cascade_chain(cached_mood_id, cascade)
    for x in decayed_chain:
        if x != cached_mood_id and x in vote_map:
            vote_map[x] = vote_map[x] + bias   # bias < 0
```

**Trace (tala, cached=creative, tenure=10, bias=−4):** after LLM `{affectionate:6, creative:13}` →
after transition `{affectionate:2, creative:9}`. Affectionate (a `affectionate→creative` feeder) lost 4;
Playful and Hopeful (also `→creative`) were hit too (`rule=−4` in the tally). This is why a genuinely
affectionate/romantic day can't surface as **Affectionate** — its feeder relationship to Creative means
it's decayed whenever Creative is the overstayed incumbent.

**Why it's wrong:** cascade runs *before* the bias, so any feeder above the cascade threshold has already
transferred to 0; the decay only ever hits *below-threshold* feeders, which can't re-cascade anyway. So it
does nothing for its stated anti-re-cascade purpose and only suppresses legitimate alternative moods.
Re-cascade prevention is already handled by the post-eviction cooldown.

**Pre-existing** (old `_decay_state` decayed the chain in `effective`); the homeostasis-v2 refactor moved
it onto `vote_map`, so it now also corrupts the reported vote counts (the visible `votes=2 vs llm=6`).

**Fix:** apply the incumbent bias to the **incumbent only**. Still compute the reverse-cascade chain (for
the cooldown-destination guard `winner_id not in feeders`, so we don't cooldown-bar a mood we just handed
off to), but don't decay it. Update `tinfo["decayed_chain"]` to reflect only what's actually decayed (the
incumbent) so `explain_mood`/the debug panel stay truthful.

**Secondary (same fix):** consider narrowing the **cooldown's** reverse-cascade barring
(`_cooldown_excluded` bars `_reverse_cascade_chain(vacated)`) for the same "don't suppress legit feeders"
reason — time-bounded, so lower priority, but same principle.

## Not bugs (config/tuning — user to retune later)
- Flirty mood scoring low under flirting *mode*: mode ≠ mood; flirty's threshold is 6 (highest of any
  mood); today's emotions are affectionate, not flirty. Plus the affectionate rule "loving & tender
  recently" isn't firing on genuinely affectionate content — worth a tuning look. All config levers.
