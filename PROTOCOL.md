# How the portal actually works

Everything here comes from poking at the running portal as a normal logged-in user: reading network requests and the JS bundles the browser downloads. Nothing was attacked. It's all read-only GETs, apart from logging in.

Where I'm guessing, I say so.

## The short version

It's a SvelteKit app. The pages you click around are server-rendered, but the data comes from a small undocumented JSON API under `/portal/*`. It looks like a list of meters, but there's a one-call bulk export behind a signed request.

| Path | What it gives you | Notes |
|---|---|---|
| `POST /login` | Session cookie | SvelteKit form action, fields `email` and `password` |
| `GET /portal/meters/search?q=&page=` | Meter list, 20 per page | Matches meter ID or serial only |
| `GET /portal/dts?page=` | Transformers, 20 per page | Only source of capacity |
| `GET /portal/meters/{id}/geo` | Lat/long as strings | |
| `GET /portal/meters/{id}/energy?from=&to=` | Readings | See quirks below |
| `GET /meters/{id}/__data.json` | Nameplate and hierarchy | SvelteKit's page data, not a real API |
| `GET /portal/keys` | The signing secret | Yes, really |
| `GET /portal/export` | All meters with everything | Needs a signature |
| `POST /api/auth/sign-out` | Logs out | |

Everything else I tried (`/portal/meters`, `/portal/hierarchy`, `/api`, `/openapi.json`, `/meters.csv`) is a 404. `robots.txt` has nothing useful.

## Getting in

- Login is a form post to `/login`, and you get a session cookie back. The sign-out path `/api/auth/sign-out` looks like the Better Auth library, but that's a guess.
- SvelteKit blocks cross-site form posts, so non-browser clients should send an `Origin` header matching the portal.
- With no session, `/portal/*` returns a tidy JSON 401: `{"error":"unauthorized","message":"A valid session is required."}`. Pages redirect to `/login` instead.
- I don't know how long a session lasts. The client doesn't try to predict it, it just logs in again whenever it sees that 401.

## The bulk export

This was the best find. The "Export all meters" button on the Transformers page is the only thing that calls it, and the code is in that page's JS bundle:

1. `GET /portal/keys` returns `{"data":{"signingSecret":"..."}}`. It's a 32-character secret handed to any logged-in user, and it stayed the same across the calls I made.
2. Build the string `METHOD\nPATH\nQUERY\nTIMESTAMP`, for example `GET\n/portal/export\npage=1\n1793817600`. The timestamp is in unix seconds, and the query has no `?`.
3. HMAC-SHA256 it with the secret and hex-encode the result.
4. Send it as `x-signature`, with the timestamp in `x-timestamp`.

What I found when testing it:

- The timestamp has to be within about 5 minutes. 299 seconds old worked, 301 failed with `{"error":"signature_invalid"}`. Being 200 seconds in the future was also fine.
- `page` is signed but does nothing. `page=2` and no page at all both return all 403 meters.
- It's about 230 KB, and has fields no other endpoint returns: full hierarchy with codes and names as objects, numeric lat/long, `build`, `installType`.

The signature doesn't protect much, since the secret comes from the same session. I guess it's there to stop casual scraping.

I compared the export with every meter's detail page. Nameplate fields match on all 403, and hierarchy differences are only how blanks get displayed. So the export is enough on its own.

## Data available

- **403 meters**, `J100000` to `J100402`. No duplicate IDs or serials.
- **40 transformers**, `DT-001` to `DT-040`, each with 10 to 13 meters.
- **Hierarchy:** Zone > Circle > Division > Subdivision > Substation > Feeder > DT. Counts are 3, 6, 10, 14, 18, 28 and 40 distinct real codes.
- **Readings:** cumulative `kwh`, `kvah` and voltage on the R phase `voltR`. June 2026 only.

## Quirks and surprises

**Rate limit, with no warning.** While exploring I fired energy requests from 4 parallel workers. 101 got through, then the other 302 all came back as `429 {"error":"rate_limited"}`. There's no `Retry-After` and no limit headers, and `/geo` was blocked at the same moment, so it isn't per-endpoint. Going one at a time at about 1 request per second, I then walked all 403 with zero 429s. The exact limit is unknown. Oddly, 403 `__data.json` page requests went through at full speed, so that limit seems to apply only to `/portal/*`.

**Dates fail silently.** `from` and `to` must be `YYYY-MM-DD`. Send anything else (`01/06/2026`, an ISO timestamp, garbage, or wrong parameter names) and you get a 200 with the default window, as if everything was fine.

| You send | You get |
|---|---|
| Nothing | Latest 7 days of data, 23/06 to 30/06 (it's November now, so "latest" is relative to the data, not today) |
| `from` only | 7 days from there |
| `to` only | Runs up to that day, 01/06 to 02/06 in my test (data starts 01/06, so this may be clipped) |
| `from` > `to`, or a range with no data | 200 with an empty list |

`to` is inclusive, so you get the whole of that day.

**Two reading frequencies, and nothing says which.** `J100000` to `J100039` report every 30 minutes (1440 rows for June). The other 363 report once a day at 00:00 (30 rows). You only find out by looking at the gaps. It doesn't line up with make, build or status.

**Duplicate rows.** Five meters (J100089, J100134, J100195, J100211, J100330) have a second row for `30/06/2026 00:00` with `kwh` and `voltR` blank. That's why they have 31 rows, not 30. The UI would show this as `—`.

**Everything's a string in readings.** Numbers are strings, blanks are `""`, and dates are `DD/MM/YYYY HH:MM`, so naive parsing reads 01/06 as January 6.

**Two nameplate formats.** The detail page data comes in two shapes, and it maps exactly to `build` in the export:

- `legacy` (238 meters): `{"data":[{"parameterName":"Serial No","parameterValue":"SE33962"}, ...]}`
- `v2` (165 meters): `{"classData":"{\"installed_meter\":{\"SerialNo\":\"SE65293\", ...}}"}`, so JSON inside a string, with different key names.

`__data.json` is also encoded in SvelteKit's format, where values are indexes into a flat array. Not fun, and the export makes it unnecessary.

**Messy hierarchy.**

- 14 values have a blank code but a name, like `{"code":"","name":"Circle 6"}`, and 8 more have a code but a blank name. Between them that's 22 holes across circle, substation and feeder only.
- Three meters (`J100400` to `J100402`) call DT-007 "Old Malviya Nagar Xfmr", while the DT list says "Sanganer DT 7". They look like they were added later, going by the IDs.
- It isn't a tree. Each circle sits under one zone, and each DT under one feeder, but in between it's many-to-many: D-01 appears under C-01, C-03 and C-05. All 10 divisions, all 14 subdivisions, all 18 substations and 12 of the 28 feeders do this.

**Small ones.**

- Serials can contain `&` (`L&84997`), so URL-encode them.
- The serial prefix doesn't reliably match the make (`SE33962` is an HPL meter).
- `/portal/meters/search` doesn't search make or anything else, `q=Genus` returns 0.
- Bad `page` values get treated as page 1 without complaint, and `pageSize` is ignored, it's always 20.
- Past the last page you get a 200 with empty `data`.
- A missing meter is a JSON 404 on `/portal/*`, but on `__data.json` it's a 200 wrapping `{"type":"error","status":404}`.
- Unknown `/portal/*` routes return an HTML 404, not JSON.
- The nav says "Ops Desk" at the top right, but that's just the logged-in user's name, not a page. It cost me a few guesses at URLs.

## What I couldn't determine

- Session lifetime, and whether the signing secret ever rotates. The client handles both by retrying once when it gets the 401.
- The exact rate limit and how long it takes to reset.
- Whether data ever changes. Readings stop at 30 June, so this may be a snapshot.
