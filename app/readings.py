"""Cleans up meter readings and works out actual consumption from them."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

# the portal never says what timezone it uses. It's a Jaipur utility and days
# start cleanly at 00:00, so IST is my best guess. Written up in the README.
IST = timezone(timedelta(hours=5, minutes=30))


def _number(value) -> float | None:
    # blanks come through as "" and everything else is a numeric string
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _merge_duplicates(raw: list[dict]) -> list[dict]:
    # 5 meters have a second row for 30/06 00:00 with kwh and voltage blank.
    # Same timestamp means same reading, so keep whichever values exist.
    merged: dict[str, dict] = {}
    for item in raw:
        seen = merged.setdefault(item["timestamp"], dict(item))
        for key, value in item.items():
            if seen.get(key) in ("", None):
                seen[key] = value
    return list(merged.values())


def clean_readings(raw: list[dict]) -> list[dict]:
    rows = []
    previous_kwh = None
    for item in _merge_duplicates(raw):
        # DD/MM/YYYY, so don't let anything guess this format
        stamp = datetime.strptime(item["timestamp"], "%d/%m/%Y %H:%M").replace(tzinfo=IST)
        kwh = _number(item.get("kwh"))
        delta = None
        # kwh is a running meter total, so usage is the difference between two
        # readings. Skipping blanks means the next delta covers the whole gap.
        if kwh is not None:
            if previous_kwh is not None:
                delta = round(kwh - previous_kwh, 3)
            previous_kwh = kwh
        rows.append({
            "timestamp": stamp.isoformat(),
            "kwh_total": kwh,
            "kvah_total": _number(item.get("kvah")),
            "voltage_r": _number(item.get("voltR")),
            "kwh_delta": delta,
        })
    return rows


def daily_consumption(rows: list[dict], start_day: str | None = None) -> list[dict]:
    """Per-day kWh. Each delta goes to the day its reading landed on."""
    days = defaultdict(lambda: {"kwh": 0.0, "reading_count": 0})
    for row in rows:
        if row["kwh_delta"] is None:
            continue
        day = row["timestamp"][:10]
        days[day]["kwh"] += row["kwh_delta"]
        days[day]["reading_count"] += 1
    return [
        {"date": day, "kwh": round(values["kwh"], 3), "reading_count": values["reading_count"]}
        for day, values in sorted(days.items())
        if start_day is None or day >= start_day
    ]
