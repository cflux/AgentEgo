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
    last_ts = await get_last_activity_ts(profile, db_path=db_path)
    if not last_ts:
        return {}, None  # never talked → no baseline to feel absence against
    idle_min = max(0.0, (time.time() - last_ts) / 60.0)
    hours_over = max(0.0, (idle_min - cfg["onset_min"]) / 60.0)
    if hours_over <= 0:
        return {}, None

    pressure = min(cfg["cap"], cfg["rate_per_hour"] * hours_over)
    if pressure < 0.05:  # just past onset — not worth a vote or a breakdown line yet
        return {}, None
    votes = {mid: round(pressure * w, 3) for mid, w in cfg["targets"].items() if pressure * w > 0}
    if not votes:
        return {}, None
    top = max(votes, key=votes.get)
    hrs = idle_min / 60.0
    note = f"Solitude: {top} +{votes[top]:.1f} (alone {hrs:.1f} h)"
    return votes, note
