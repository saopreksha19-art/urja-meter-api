from app.network import build_index, build_tree, distance_km
from app.readings import clean_readings, daily_consumption

from .fake_portal import DTS, ENERGY, METERS


def index():
    return build_index(METERS, DTS)


def test_blank_code_is_filled_from_name():
    assert index().meters["J100011"]["network"]["circle"]["code"] == "C-01"


def test_blank_name_is_filled_from_code():
    assert index().meters["J100162"]["network"]["feeder"]["name"] == "Feeder 1"


def test_dt_list_wins_name_conflicts():
    built = index()
    assert built.meters["J100400"]["network"]["dt"]["name"] == "Sanganer DT 7"
    assert any(i["type"] == "dt_name_conflict" and i["meter_id"] == "J100400" for i in built.issues)


def test_totally_blank_node_stays_null_and_is_reported():
    built = index()
    assert built.meters["J100500"]["network"]["circle"] == {"code": None, "name": None}
    assert any(i["type"] == "unresolved" and i["meter_id"] == "J100500" for i in built.issues)


def test_shared_codes_are_reported():
    assert any(
        i["type"] == "multiple_parents" and "C-01, C-03" in i["detail"] for i in index().issues
    )


def test_tree_counts_add_up():
    tree = build_tree(index().meters.values())
    assert sum(zone["meter_count"] for zone in tree) == len(METERS)
    circles = tree[0]["children"]
    assert sum(c["meter_count"] for c in circles) == len(METERS)


def test_distance_is_about_right():
    # one degree of latitude is roughly 111 km
    assert 110 < distance_km(26, 75, 27, 75) < 112


def test_readings_parse_dates_and_handle_blanks():
    rows = clean_readings(ENERGY)
    assert rows[0]["timestamp"] == "2026-06-01T23:30:00+05:30"
    assert rows[0]["kwh_delta"] is None
    assert rows[1]["kwh_delta"] == 0.5
    assert rows[2]["kwh_total"] is None
    # delta after the blank covers the whole gap
    assert rows[3]["kwh_delta"] == 1.0


def test_daily_consumption_drops_the_lead_in_day():
    days = daily_consumption(clean_readings(ENERGY), "2026-06-02")
    assert days == [
        {"date": "2026-06-02", "kwh": 1.5, "reading_count": 2},
        {"date": "2026-06-03", "kwh": 1.5, "reading_count": 1},
    ]


def test_duplicate_timestamp_row_is_merged():
    # real example from meter J100089
    rows = clean_readings([
        {"timestamp": "30/06/2026 00:00", "kwh": "12732.34", "kvah": "13750.93", "voltR": "229"},
        {"timestamp": "30/06/2026 00:00", "kwh": "", "kvah": "13750.93", "voltR": ""},
    ])
    assert len(rows) == 1
    assert rows[0]["kwh_total"] == 12732.34
