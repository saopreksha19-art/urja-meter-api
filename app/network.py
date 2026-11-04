"""Turns the raw export into something clean we can query.

The export is mostly fine but has holes in the hierarchy, so all the cleanup
happens here, once. Anything we had to fix goes into `issues` so that people
can see it instead of us silently hiding it.
"""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

LEVELS = ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]


@dataclass
class Index:
    meters: dict[str, dict] = field(default_factory=dict)
    transformers: dict[str, dict] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)


def build_index(raw_meters: list[dict], raw_transformers: list[dict]) -> Index:
    index = Index()
    # the /portal/dts list is the only place DT names look properly maintained,
    # so it wins whenever a meter disagrees with it
    official_dts = {t["code"]: t for t in raw_transformers}
    names_for_code, codes_for_name = _lookups(raw_meters)

    for raw in raw_meters:
        path = {}
        for level in LEVELS:
            path[level] = _clean_node(
                raw["meterId"], level, raw["hierarchy"].get(level) or {},
                names_for_code[level], codes_for_name[level], index.issues,
            )

        official = official_dts.get(raw["dtCode"])
        if official and path["dt"]["name"] != official["name"]:
            index.issues.append({
                "type": "dt_name_conflict",
                "meter_id": raw["meterId"],
                "level": "dt",
                "detail": f"meter says '{path['dt']['name']}', "
                          f"{raw['dtCode']} is officially '{official['name']}'",
            })
            path["dt"]["name"] = official["name"]

        index.meters[raw["meterId"]] = {
            "meter_id": raw["meterId"],
            "serial_no": raw["serialNo"],
            "make": raw["make"],
            "phase_type": raw["phaseType"],
            "install_status": raw["installStatus"],
            "install_type": raw["installType"],
            "location": {"lat": raw["geo"]["lat"], "lng": raw["geo"]["lng"]},
            "network": path,
        }

    counts = Counter(m["network"]["dt"]["code"] for m in index.meters.values())
    for t in raw_transformers:
        index.transformers[t["code"]] = {
            "code": t["code"],
            "name": t["name"],
            "feeder_code": t["feederCode"],
            "capacity_kva": t["capacityKva"],
            "meter_count": counts[t["code"]],
        }

    index.issues.extend(_shared_codes(index.meters))
    return index


def _lookups(raw_meters):
    # count what each code/name pair looks like across all meters, so one meter
    # with a hole can borrow the answer from its neighbours
    names_for_code = {level: defaultdict(Counter) for level in LEVELS}
    codes_for_name = {level: defaultdict(Counter) for level in LEVELS}
    for raw in raw_meters:
        for level in LEVELS:
            node = raw["hierarchy"].get(level) or {}
            code, name = node.get("code"), node.get("name")
            if code and name:
                names_for_code[level][code][name] += 1
                codes_for_name[level][name][code] += 1
    return names_for_code, codes_for_name


def _clean_node(meter_id, level, node, names_for_code, codes_for_name, issues) -> dict:
    code = node.get("code") or None
    name = node.get("name") or None

    if code and name:
        return {"code": code, "name": name}

    if name and not code and name in codes_for_name:
        code = codes_for_name[name].most_common(1)[0][0]
        issues.append(_repair(meter_id, level, f"blank code, filled in {code} from name '{name}'"))
    elif code and not name and code in names_for_code:
        name = names_for_code[code].most_common(1)[0][0]
        issues.append(_repair(meter_id, level, f"blank name, filled in '{name}' from code {code}"))
    else:
        issues.append({
            "type": "unresolved",
            "meter_id": meter_id,
            "level": level,
            "detail": "couldn't work this one out, leaving it null",
        })
    return {"code": code, "name": name}


def _repair(meter_id, level, detail) -> dict:
    return {"type": "repaired", "meter_id": meter_id, "level": level, "detail": detail}


def _shared_codes(meters: dict[str, dict]) -> list[dict]:
    # e.g. D-01 sits under C-01, C-03 and C-05. So this isn't really a strict
    # tree. We don't try to pick a winner, we just report it.
    parents = defaultdict(set)
    for meter in meters.values():
        for parent_level, level in zip(LEVELS, LEVELS[1:]):
            child = meter["network"][level]["code"]
            parent = meter["network"][parent_level]["code"]
            if child and parent:
                parents[(level, child)].add(parent)
    return [
        {
            "type": "multiple_parents",
            "meter_id": None,
            "level": level,
            "detail": f"{code} appears under {', '.join(sorted(found))}",
        }
        for (level, code), found in sorted(parents.items())
        if len(found) > 1
    ]


def build_tree(meters) -> list[dict]:
    """Tree built from each meter's path, so one code can show up under
    several parents. Each spot only counts the meters that really sit there."""
    root: dict = {"children": {}}
    for meter in meters:
        node = root
        for level in LEVELS:
            item = meter["network"][level]
            key = item["code"] or f"unknown:{item['name']}"
            child = node["children"].get(key)
            if child is None:
                child = {**item, "level": level, "meter_count": 0, "children": {}}
                node["children"][key] = child
            child["meter_count"] += 1
            node = child
    return _as_lists(root)["children"]


def _as_lists(node: dict) -> dict:
    children = [_as_lists(c) for _, c in sorted(node["children"].items())]
    return {**node, "children": children}


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    # haversine, plenty accurate over a single city
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))
