"""Solitude pressure — Phase 4 (the loop-damper).

Time alone feeds votes toward lonely/bored/tired. Folded into the v2 mood vote map at eval time, so being
left alone becomes a genuinely *felt* mood — which the impulse arbiter then reads and converts into an
outward reach-out. This self-regulates the Phase-3 sidequest→mood→sidequest runaway: once she feels lonely,
the pull is back toward the user, not toward another solo action. The correction is emotional, not mechanical.

Unlike a solo round (a past event in the lookback), solitude is a *current-state* driver: its magnitude is a
function of how long she's been alone right now, so it's computed and added at vote time, not stored.
"""
import json
import logging

from .impulse_engine import get_last_activity_ts
from .settings_store import get_setting

logger = logging.getLogger(__name__)

_DEFAULT_TARGETS = {"lonely": 1.0, "bored": 0.6, "tired": 0.2}


async def get_solitude_config() -> dict:
    """Tuning knobs for the solitude driver."""
    async def _f(k, d):
        try:
            return float(await get_setting(k, str(d)))
        except (TypeError, ValueError):
            return d
    try:
        targets = json.loads(await get_setting("solitude_targets", "") or "{}")
        if not isinstance(targets, dict) or not targets:
            targets = dict(_DEFAULT_TARGETS)
    except (ValueError, TypeError):
        targets = dict(_DEFAULT_TARGETS)
    return {
        "enabled": (await get_setting("solitude_enabled", "1")) == "1",
        "onset_min": await _f("solitude_onset_min", 90.0),
        "rate_per_hour": await _f("solitude_rate_per_hour", 2.0),
        "cap": await _f("solitude_cap", 6.0),
        "targets": {k: float(v) for k, v in targets.items()},
        "ignored_bump": await _f("solitude_ignored_bump", 3.0),
        "ignored_halflife_hours": max(0.1, await _f("solitude_ignored_halflife_hours", 6.0)),
    }


async def solitude_votes(profile: str, db_path: str | None = None,
                         cfg: dict | None = None) -> tuple[dict, str | None]:
    """Current solitude pressure as mood votes (shaping-layer units) + a breakdown line.

    Empty while recently engaged (idle < onset) or with no activity baseline. Ramps linearly past the onset
    at `rate_per_hour`, capped at `cap`, then split across the target moods by weight.
    """
    if cfg is None:
        cfg = await get_solitude_config()
    if not cfg["enabled"]:
        return {}, None

    import time
    now = time.time()
    last_ts = await get_last_activity_ts(profile, db_path=db_path)
    idle_min = max(0.0, (now - last_ts) / 60.0) if last_ts else 0.0
    hours_over = max(0.0, (idle_min - cfg["onset_min"]) / 60.0)
    # Idle pressure ramps past the onset, capped — pure solitude alone never reaches the lonely->sad cascade.
    idle_pressure = min(cfg["cap"], cfg["rate_per_hour"] * hours_over) if (last_ts and hours_over > 0) else 0.0

    # Phase 5 — the sting of being ignored: each unanswered reach-out (since he was last around) adds a
    # decaying bump ON TOP of the idle cap, so being rebuffed pushes Lonely past the cascade (pure idle can't).
    sting = 0.0
    n_ignored = 0
    if cfg["ignored_bump"] > 0:
        try:
            from .impulse_feedback import ignored_pings_since_reengage
            ig = await ignored_pings_since_reengage(profile, db_path=db_path)
        except Exception:
            ig = []
        n_ignored = len(ig)
        hl = cfg["ignored_halflife_hours"]
        sting = sum(cfg["ignored_bump"] * 0.5 ** (((now - t) / 3600.0) / hl) for t in ig)
        sting = min(sting, 2.0 * cfg["ignored_bump"])  # bound a burst of rebuffs

    total = idle_pressure + sting
    if total < 0.05:
        return {}, None
    votes = {mid: round(total * w, 3) for mid, w in cfg["targets"].items() if total * w > 0}
    if not votes:
        return {}, None
    top = max(votes, key=votes.get)
    hrs = idle_min / 60.0
    extra = f", {n_ignored} ignored" if n_ignored else ""
    note = f"Solitude: {top} +{votes[top]:.1f} (alone {hrs:.1f} h{extra})"
    return votes, note
