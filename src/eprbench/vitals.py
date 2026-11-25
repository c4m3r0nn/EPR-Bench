"""Deterministic bedside-monitor signals.

A signal is a pure function of (track parameters, time): a piecewise-linear
clinical trajectory plus seeded noise. Nothing is stored per second, so a
patient can be "monitored" for days at zero storage cost and any instant can
be sampled in O(log keyframes).
"""

import json
from bisect import bisect_right
from functools import lru_cache
from hashlib import blake2b

_WANDER_PERIOD = 30  # seconds between knots of the slow, correlated component


def _unit_noise(seed: int, signal: str, t: int) -> float:
    """Approximately N(0, 1), fully determined by its arguments."""
    digest = blake2b(f"{seed}:{signal}:{t}".encode(), digest_size=8).digest()
    total = sum(int.from_bytes(digest[i : i + 2], "big") for i in range(0, 8, 2)) / 65535
    return (total - 2.0) * 3**0.5


@lru_cache(maxsize=1024)
def _keyframes(raw: str) -> tuple[tuple[int, ...], tuple[float, ...]]:
    frames = json.loads(raw)
    return tuple(f[0] for f in frames), tuple(f[1] for f in frames)


def trend_at(raw: str, t: int) -> float:
    """The noise-free clinical trajectory at time t."""
    times, values = _keyframes(raw)
    i = bisect_right(times, t)
    if i == 0:
        return values[0]
    if i == len(times):
        return values[-1]
    t0, t1 = times[i - 1], times[i]
    return values[i - 1] + (values[i] - values[i - 1]) * (t - t0) / (t1 - t0)


def value_at(track: dict, t: int) -> float:
    """Sample a vitals_track row at simulated time t."""
    seed, signal, sd = track["seed"], track["signal"], track["sd"]
    knot, frac = divmod(t, _WANDER_PERIOD)
    frac /= _WANDER_PERIOD
    wander = _unit_noise(seed, signal + "/w", knot) * (1 - frac) + _unit_noise(seed, signal + "/w", knot + 1) * frac
    value = trend_at(track["keyframes"], t) + sd * (0.8 * wander + 0.3 * _unit_noise(seed, signal, t))
    if track["lo"] is not None:
        value = max(track["lo"], value)
    if track["hi"] is not None:
        value = min(track["hi"], value)
    return round(value, track["decimals"]) if track["decimals"] else float(round(value))
