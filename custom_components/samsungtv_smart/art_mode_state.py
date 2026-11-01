"""Resolve Art Mode from shared status and recent events."""

from __future__ import annotations

import math
from typing import Any

# Poll responses can lag behind mode-change events.
ART_BROADCAST_GRACE = 20.0


def shared_panel_art_mode(
    coordinator: Any, *, now: float, poll_interval: float
) -> tuple[bool, float] | None:
    """Return the mode and request time from a fresh panel reading."""
    if not getattr(coordinator, "last_update_success", False):
        return None
    snapshot = getattr(coordinator, "data", None)
    if not isinstance(snapshot, dict) or snapshot.get("powered_off"):
        return None
    tv = snapshot.get("tv")
    mode = tv.get("pictureMode") if isinstance(tv, dict) else None
    observed_at = snapshot.get("polled_at")
    if (
        not isinstance(mode, str)
        or not mode.strip()
        or mode.strip().casefold() in {"unknown", "unavailable", "none", "null"}
        or type(observed_at) not in (int, float)
        or not math.isfinite(observed_at)
        or not 0 <= now - observed_at <= max(30.0, 2 * poll_interval)
    ):
        return None
    return mode.strip() == "Ambient", observed_at


def art_broadcast_override(
    observed_at: float | None,
    broadcast: tuple[bool, float] | None,
    *,
    now: float,
    grace: float = ART_BROADCAST_GRACE,
) -> bool | None:
    if (
        observed_at is None
        or not isinstance(broadcast, (tuple, list))
        or len(broadcast) != 2
        or not isinstance(broadcast[0], bool)
        or type(broadcast[1]) not in (int, float)
        or not math.isfinite(broadcast[1])
    ):
        return None
    art_on, heard_at = broadcast
    if observed_at >= heard_at + grace or not 0 <= now - heard_at < grace:
        return None
    return art_on
