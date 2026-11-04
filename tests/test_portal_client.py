EXPECTED_SIG = "ca9432c552aec7176b799435765198875b951599f2ffb15bc65d8845050c985b"

import pytest

from app.portal import PortalAuthError, PortalClient, PortalError, PortalNotFound, sign_request

from .fake_portal import FakePortal


def make(portal: FakePortal, password="right", attempts=4) -> PortalClient:
    return PortalClient(
        portal.client(), "a@b.c", password,
        min_gap_seconds=0, max_attempts=attempts, backoff_base_seconds=0,
    )


def test_signature_matches_a_known_value():
    # worked out separately with `openssl dgst -sha256 -hmac`, so this isn't
    # just the code agreeing with itself
    assert sign_request("key", "GET", "/portal/export", "page=1", "1700000000") == EXPECTED_SIG


async def test_export_logs_in_and_signs():
    portal = FakePortal()
    meters = await make(portal).export_meters()
    assert len(meters) == 6
    assert portal.logins == 1


async def test_wrong_password_is_a_clear_error():
    with pytest.raises(PortalAuthError):
        await make(FakePortal(), password="wrong").login()


async def test_logs_back_in_when_session_expires():
    portal = FakePortal()
    client = make(portal)
    await client.list_transformers()
    portal.session_valid = False
    assert len(await client.list_transformers()) == 2
    assert portal.logins == 2


async def test_refetches_secret_when_it_rotates():
    portal = FakePortal()
    client = make(portal)
    await client.export_meters()
    portal.secret = "rotated"
    assert len(await client.export_meters()) == 6


async def test_rides_out_rate_limits_and_500s():
    portal = FakePortal()
    client = make(portal)
    await client.login()
    portal.fail_next = [429, 500, 429]
    assert len(await client.readings("J100000", None, None)) == 5


async def test_gives_up_eventually():
    portal = FakePortal()
    client = make(portal, attempts=2)
    await client.login()
    portal.fail_next = [429] * 10
    with pytest.raises(PortalError):
        await client.readings("J100000", None, None)


async def test_missing_meter_raises_not_found():
    portal = FakePortal()
    client = make(portal)
    with pytest.raises(PortalNotFound):
        await client.readings("J999999", None, None)
