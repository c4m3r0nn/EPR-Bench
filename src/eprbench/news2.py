"""NEWS2 (Royal College of Physicians, 2017) aggregate score."""


def _band(value: float, bands: list[tuple[float, int]], above: int) -> int:
    """Score of the first band whose upper bound is >= value, else `above`."""
    for upper, score in bands:
        if value <= upper:
            return score
    return above


def _spo2(spo2: float, on_o2: bool, scale: int) -> int:
    if scale == 1:
        return _band(spo2, [(91, 3), (93, 2), (95, 1)], 0)
    if spo2 <= 92 or not on_o2:
        return _band(spo2, [(83, 3), (85, 2), (87, 1)], 0)
    return _band(spo2, [(94, 1), (96, 2)], 3)


def news2(obs: dict, spo2_scale: int = 1) -> dict | None:
    """Score one observation set. Returns None unless all seven parameters are present.

    `obs` maps observation kinds to values: RR, SPO2, ON_O2 (0/1), SBP, HR,
    ACVPU ("A", "C", "V", "P" or "U") and TEMP.
    """
    required = ("RR", "SPO2", "ON_O2", "SBP", "HR", "ACVPU", "TEMP")
    if any(obs.get(k) is None for k in required):
        return None
    on_o2 = bool(obs["ON_O2"])
    parts = {
        "RR": _band(obs["RR"], [(8, 3), (11, 1), (20, 0), (24, 2)], 3),
        "SPO2": _spo2(obs["SPO2"], on_o2, spo2_scale),
        "ON_O2": 2 if on_o2 else 0,
        "SBP": _band(obs["SBP"], [(90, 3), (100, 2), (110, 1), (219, 0)], 3),
        "HR": _band(obs["HR"], [(40, 3), (50, 1), (90, 0), (110, 1), (130, 2)], 3),
        "ACVPU": 0 if str(obs["ACVPU"]).upper() == "A" else 3,
        "TEMP": _band(obs["TEMP"], [(35.0, 3), (36.0, 1), (38.0, 0), (39.0, 1)], 2),
    }
    total = sum(parts.values())
    if total >= 7:
        risk = "high"
    elif total >= 5:
        risk = "medium"
    elif 3 in parts.values():
        risk = "low-medium"
    else:
        risk = "low"
    return {"total": total, "risk": risk, "parts": parts}
