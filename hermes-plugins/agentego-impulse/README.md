# agentego-impulse (Hermes plugin)

The "hands" of the Impulse v2 system (see `docs/impulse_agency_plan.md`). Bridges impulse cron turns
to AgentEgo — no Hermes source changes.

- **pre_llm_call** — on an impulse cron turn, fetches AgentEgo's context brief
  (`/api/impulse/brief`) and injects it (the cron session is cold: no mnemosyne, no live history).
- **post_llm_call** — forwards the outcome to AgentEgo (`/api/impulse/outcome`) for sidequest
  scoring, and for OUTWARD impulses (prompt marker `[IMPULSE-OUTWARD]`) mirrors the message into the
  user's DM session transcript so the agent remembers it. Hermes' native `deliver=telegram:<dm>`
  performs the send; this adds the mirror that auto-delivery omits.

Both hooks are cron-gated (`platform=="cron"`) — inert for live gateway turns.

## Deploy (per profile)
```
cp -r hermes-plugins/agentego-impulse ~/.hermes/profiles/<profile>/plugins/
hermes -p <profile> plugins enable agentego-impulse
hermes -p <profile> config set cron.wrap_response false   # clean outward DMs
hermes -p <profile> gateway restart                        # load the plugin
```
Optional env: `IMPULSE_DM_CHAT_ID` (override the auto-resolved DM target), `EGO_URL`
(default `http://127.0.0.1:8765`).
