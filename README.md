# Urja Meter API

Flock's take-home. Urja Meter Ops is an old internal portal for looking up smart meters, and it has no API, so people copy-paste out of the browser. This is a small read-only service that sits in front of it and gives you clean JSON instead.

Nobody using this needs to know the portal exists. No logins, no signed requests, no `DD/MM/YYYY` strings.

- How the portal really works: [PROTOCOL.md](PROTOCOL.md) (the interesting read)
- API spec: [openapi.json](openapi.json), or browse it at `/docs` once it's running
- Answers to the reflection questions: [REFLECTION.md](REFLECTION.md)

## Running it

You need Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env        # then put the portal password from the email in .env

python -m scripts.smoke     # optional: checks everything against the real portal
python -m uvicorn app.main:app --port 8000
```

Then open http://localhost:8000/docs.

Tests don't need the password or a network, they use a fake portal:

```bash
pytest
```

## Try it

The first request takes a couple of seconds because it logs in and pulls the export. After that it's instant.

```bash
curl "http://localhost:8000/v1/meters?make=genus&install_status=faulty&page_size=1"
```

```json
{
  "data": [{
    "meter_id": "J100004",
    "serial_no": "SE65293",
    "make": "Genus",
    "phase_type": "single",
    "install_status": "Faulty",
    "install_type": "CT Operated",
    "location": {"lat": 26.907425164505934, "lng": 75.72503661059095},
    "network": {
      "zone": {"code": "Z-02", "name": "Jaipur Zone 2"},
      "circle": {"code": "C-05", "name": "Circle 5"},
      "division": {"code": "D-05", "name": "Division 5"},
      "subdivision": {"code": "SD-05", "name": "Subdivision 5"},
      "substation": {"code": "SS-05", "name": "Substation 5"},
      "feeder": {"code": "F-005", "name": "Feeder 5"},
      "dt": {"code": "DT-005", "name": "Bani Park DT 5"}
    },
    "distance_km": null
  }],
  "meta": {"as_of": "2026-11-04T18:50:00Z", "stale": false, "total": 20, "page": 1, "page_size": 1}
}
```

| Endpoint | What you get |
|---|---|
| `GET /v1/meters` | Search and filter on `q`, make, phase, status, install type or any network level. Add `lat`, `lng`, `radius_km` for "what's near here", nearest first |
| `GET /v1/meters/{id}` | One meter |
| `GET /v1/meters/{id}/readings?from=&to=` | Readings with real timestamps, real numbers and the kWh used since the last reading |
| `GET /v1/meters/{id}/consumption?from=&to=` | kWh per day |
| `GET /v1/transformers`, `/v1/transformers/{code}` | Transformers with their meter counts |
| `GET /v1/network/tree` | Zone down to transformer, with meter counts |
| `GET /v1/network/issues` | Everything that's wrong with the hierarchy data, and what we did about it |
| `POST /v1/refresh` | Re-pull the data now |
| `GET /health` | Is it up, how old is the data. Never touches the portal |

Errors always look like `{"error": {"code": "...", "message": "..."}}`.

## How it's laid out

```
app/portal.py    talks to the portal: login, signing, pacing, retries
app/network.py   turns the raw export into a clean index, repairs the hierarchy
app/readings.py  cleans readings, works out consumption
app/store.py     caching and stale fallback
app/main.py      the HTTP routes
app/schemas.py   response models, which is also where openapi.json comes from
tests/           fake portal plus tests
scripts/         smoke test against the real portal, openapi generator
```

A request goes `main -> store -> portal`. Only `portal.py` knows what the portal looks like, so if it changes, that's the one file to fix.

## Assumptions

- **Timestamps are IST.** The portal never says. It's a Jaipur utility and days start at a clean 00:00, so I went with `+05:30`. If that's wrong it's a one-line fix, but every timestamp would have been off.
- **`kwh` is a running meter total, not usage per interval.** It only ever goes up across all 403 meters, so usage is the difference between two readings.
- **The `/portal/dts` list is right about transformer names.** Three meters call DT-007 "Old Malviya Nagar Xfmr" while everything else says "Sanganer DT 7". I went with the list and flagged those three.
- **Read-only and trusted network.** The API has no auth of its own. It's assumed to sit somewhere internal.
- **One service instance.** Caches are in memory.

## Decisions and trade-offs

**Use the export for all meter data, instead of scraping meter pages.** The portal has a hidden bulk export behind a signed request. One call gets all 403 meters, against 403 calls for the pages. Before relying on it I compared it field by field with every meter's detail page, and nameplate and hierarchy values all matched. So I also skipped parsing the detail pages and their two different formats, which was a nice chunk of code not to write.

**Keep everything in memory, in a plain dict.** 403 meters scanned in a loop is plenty fast, and a database would be more to run for no gain. It'd start to struggle somewhere in the tens of thousands of meters, mainly the export itself (about 230 KB today, so around 57 MB at 100k) and the full scan on every filter. At that point I'd move to SQLite or Postgres, with PostGIS or an R-tree for location search, and refresh in the background.

**Fix the hierarchy where it's safe, report everything.** A blank code gets filled in if its name maps to a code elsewhere, and the other way round. Every change shows up in `/v1/network/issues`. Nothing gets dropped.

**Don't pretend the network is a strict tree.** D-01 sits under C-01, C-03 and C-05, and F-001 sits under SS-01 and SS-11. Lots of codes do this, so forcing one parent would be making things up. My reading is that these codes might only be unique within their parent, but I can't confirm that from here. The tree is built from each meter's actual path, so a code can appear in several places, and each place only counts its own meters.

**Freshness is a plain TTL.** 15 minutes for the meter index, 5 for readings. The portal has no ETags or "last updated" I could use. If a refresh fails we keep serving the old data with `stale: true`, and only return a 503 when there's nothing cached at all. Every response has `meta.as_of` so callers can decide for themselves.

**Be strict about dates.** The portal silently ignores dates it can't parse and sends back the default window, which looks like success but is the wrong data. We return a 400 instead.

**Slow and polite beats fast and clever.** One portal call at a time, 0.7s apart. The rate limit is undocumented and I hit it hard while exploring (see PROTOCOL.md). At this pace I walked all 403 meters without a single 429. Backoff is still there in case.

**FastAPI** because it generates the OpenAPI spec from the code. `openapi.json` is a generated file, and a test fails if it goes out of date.

## What I left out, on purpose

- **The web client.** It'd be fun, but you'd be judging my charts. I spent the time on understanding the portal instead.
- **Transformer or feeder level consumption.** There's no bulk readings path, so one transformer means one call per meter, and a whole feeder could take minutes at this pace. It needs a background sync and storage to be any good, and a half-working version didn't seem useful.
- **Auth, metrics, Docker.** Real services need them, but they wouldn't tell you much here.
- **Parsing the detail pages.** The export has everything they have.

## With more time

- Sync readings into SQLite in the background. That unlocks fleet-wide questions like "which transformers are overloaded?" or "which meters stopped reporting?"
- Add Docker and a real health check that notices the portal's shape changing.
- Add fixtures recorded from the real portal, and a contract test that runs them on a schedule.
- Ask Flock what the codes really mean, because the multi-parent thing is the biggest open question.

## Tools

I used Claude as a pair while building this, for exploring the portal and writing the code and docs. I've gone through it and can walk you through any of it.
