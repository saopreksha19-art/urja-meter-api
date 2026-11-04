"""Quick check against the REAL portal, no server needed.

Tests use a fake portal, so this is what proves the real thing still works.
Run from the repo root once .env is filled in: python -m scripts.smoke
"""

import asyncio

import httpx

from app.config import Settings
from app.network import build_index
from app.portal import PortalClient
from app.readings import clean_readings, daily_consumption


async def main() -> None:
    settings = Settings()
    async with httpx.AsyncClient(base_url=settings.portal_url, timeout=30) as http:
        portal = PortalClient(
            http, settings.portal_email, settings.portal_password.get_secret_value()
        )
        await portal.login()
        print("login            ok")

        meters = await portal.export_meters()
        print(f"signed export    ok, {len(meters)} meters")

        transformers = await portal.list_transformers()
        print(f"transformers     ok, {len(transformers)}")

        index = build_index(meters, transformers)
        kinds: dict[str, int] = {}
        for issue in index.issues:
            kinds[issue["type"]] = kinds.get(issue["type"], 0) + 1
        print(f"issues found     {kinds}")

        rows = clean_readings(await portal.readings("J100000", "2026-06-01", "2026-06-02"))
        print(f"readings         ok, {len(rows)} rows, first {rows[0]['timestamp']}")
        print(f"daily kWh        {daily_consumption(rows)}")

        try:
            await portal.readings("J999999", None, None)
            print("unknown meter    NOT OK, expected a 404")
        except Exception as exc:
            print(f"unknown meter    ok ({type(exc).__name__})")


if __name__ == "__main__":
    asyncio.run(main())
