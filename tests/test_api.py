import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.portal import PortalClient
from app.store import Store

from .fake_portal import FakePortal


@pytest.fixture
def portal():
    return FakePortal()


@pytest.fixture
def store(portal):
    client = PortalClient(portal.client(), "a@b.c", "right", min_gap_seconds=0,
                          max_attempts=2, backoff_base_seconds=0)
    return Store(client, index_ttl=900, readings_ttl=300)


@pytest.fixture
def api(store):
    with TestClient(create_app(store)) as client:
        yield client


def test_filters_combine(api):
    body = api.get("/v1/meters", params={"make": "genus", "install_status": "installed"}).json()
    assert {m["meter_id"] for m in body["data"]} == {"J100001", "J100400"}
    assert body["meta"]["total"] == 2


def test_search_handles_ampersand_serial(api):
    body = api.get("/v1/meters", params={"q": "l&84"}).json()
    assert [m["meter_id"] for m in body["data"]] == ["J100001"]


def test_filter_by_repaired_network_code(api):
    body = api.get("/v1/meters", params={"circle": "C-01"}).json()
    assert "J100011" in {m["meter_id"] for m in body["data"]}


def test_near_sorts_closest_first_and_respects_radius(api):
    body = api.get("/v1/meters", params={"lat": 27.03, "lng": 75.91, "radius_km": 5}).json()
    assert [m["meter_id"] for m in body["data"]] == ["J100001"]
    assert body["data"][0]["distance_km"] == 0


def test_partial_geo_is_rejected(api):
    response = api.get("/v1/meters", params={"lat": 27})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_paging(api):
    body = api.get("/v1/meters", params={"page": 2, "page_size": 4}).json()
    assert len(body["data"]) == 2
    assert body["meta"]["total"] == 6


def test_unknown_meter_is_404_without_calling_portal(api, portal):
    api.get("/v1/meters")
    before = len(portal.calls)
    response = api.get("/v1/meters/J999999/readings")
    assert response.status_code == 404
    assert len(portal.calls) == before


def test_meter_id_is_case_insensitive(api):
    assert api.get("/v1/meters/j100000").json()["data"]["meter_id"] == "J100000"


def test_readings_cached(api, portal):
    api.get("/v1/meters/J100000/readings")
    api.get("/v1/meters/J100000/readings")
    assert sum(c.endswith("/energy") for c in portal.calls) == 1


@pytest.mark.parametrize("params", [
    {"from": "2026-06-01"},
    {"from": "02/06/2026", "to": "03/06/2026"},
    {"from": "2026-06-05", "to": "2026-06-01"},
])
def test_bad_dates_are_rejected_not_ignored(api, params):
    assert api.get("/v1/meters/J100000/readings", params=params).status_code == 400


def test_consumption(api):
    body = api.get(
        "/v1/meters/J100000/consumption", params={"from": "2026-06-02", "to": "2026-06-03"}
    ).json()
    assert [d["kwh"] for d in body["data"]] == [1.5, 1.5]


def test_transformers_have_counts(api):
    body = api.get("/v1/transformers/dt-007").json()
    assert body["data"]["meter_count"] == 2


def test_tree_and_issues(api):
    assert api.get("/v1/network/tree").json()["data"][0]["meter_count"] == 6
    assert api.get("/v1/network/issues").json()["meta"]["total"] > 0


def test_serves_stale_when_portal_dies(api, portal, store):
    assert api.get("/v1/meters").status_code == 200
    store._index_loaded_at = 0  # pretend the TTL ran out
    portal.fail_next = [500] * 20
    body = api.get("/v1/meters").json()
    assert body["meta"]["stale"] is True
    assert len(body["data"]) == 6
    assert api.get("/health").json()["status"] == "degraded"


def test_503_when_portal_down_and_nothing_cached(store, portal):
    with TestClient(create_app(store)) as client:
        portal.session_valid = True
        portal.fail_next = [500] * 20
        response = client.get("/v1/meters")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "portal_unavailable"


def test_openapi_file_is_up_to_date():
    # fails if someone changes the API and forgets to regenerate the file
    on_disk = json.loads(Path("openapi.json").read_text())
    assert on_disk == create_app().openapi()


def test_spec_documents_the_errors_we_really_return():
    spec = create_app().openapi()
    responses = spec["paths"]["/v1/meters"]["get"]["responses"]
    assert "422" not in responses
    assert "ErrorResponse" in responses["400"]["content"]["application/json"]["schema"]["$ref"]
