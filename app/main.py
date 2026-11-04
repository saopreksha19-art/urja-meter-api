from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from . import schemas
from .config import Settings
from .network import build_tree, distance_km
from .portal import PortalAuthError, PortalClient, PortalError, PortalNotFound
from .readings import clean_readings, daily_consumption
from .store import Store

PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=500)]


def make_store(settings: Settings) -> tuple[Store, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        base_url=settings.portal_url, timeout=settings.portal_timeout_seconds
    )
    portal = PortalClient(
        http,
        settings.portal_email,
        settings.portal_password.get_secret_value(),
        min_gap_seconds=settings.portal_min_gap_seconds,
    )
    return Store(portal, settings.index_ttl_seconds, settings.readings_ttl_seconds), http


def create_app(store: Store | None = None) -> FastAPI:
    """Tests pass in their own store that points at a fake portal."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = None
        if store is None:
            app.state.store, http = make_store(Settings())
        else:
            app.state.store = store
        yield
        if http:
            await http.aclose()

    app = FastAPI(
        title="Urja Meter API",
        version="1.0.0",
        description="A clean read-only API in front of the Urja Meter Ops portal.",
        lifespan=lifespan,
    )
    _add_error_handlers(app)
    _add_routes(app)
    _use_our_error_format_in_docs(app)
    return app


def _use_our_error_format_in_docs(app: FastAPI) -> None:
    # FastAPI documents bad input as its own 422, but our handler below returns
    # a 400 in our usual error format. Make the spec say what really happens.
    def openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        spec = get_openapi(
            title=app.title, version=app.version,
            description=app.description, routes=app.routes,
        )
        error = {
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
            }
        }
        for path, operations in spec["paths"].items():
            for operation in operations.values():
                responses = operation["responses"]
                if responses.pop("422", None):
                    responses["400"] = {**error, "description": "Bad input"}
                if path != "/health":
                    responses["503"] = {**error, "description": "Portal unavailable"}
        for unused in ("HTTPValidationError", "ValidationError"):
            spec["components"]["schemas"].pop(unused, None)
        app.openapi_schema = spec
        return spec

    app.openapi = openapi


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def _add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PortalAuthError)
    async def _auth(_: Request, exc: PortalAuthError):
        # our config is wrong, not the caller's request
        return _error(502, "portal_login_failed", str(exc))

    @app.exception_handler(PortalNotFound)
    async def _not_found(_: Request, __: PortalNotFound):
        return _error(404, "not_found", "The portal doesn't know this meter.")

    @app.exception_handler(PortalError)
    async def _portal(_: Request, exc: PortalError):
        return _error(
            503, "portal_unavailable", f"Portal isn't answering: {exc}", {"Retry-After": "30"}
        )

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        code = {404: "not_found", 400: "bad_request"}.get(exc.status_code, "error")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"][1:])
        return _error(400, "bad_request", f"{where}: {first['msg']}")


def _meta(store: Store, **extra) -> dict:
    as_of = datetime.fromtimestamp(store.as_of, timezone.utc) if store.as_of else None
    return {"as_of": as_of, "stale": store.stale, **extra}


def _matches(meter: dict, value: str | None, field: str) -> bool:
    return value is None or meter[field].lower() == value.lower()


def _add_routes(app: FastAPI) -> None:
    errors = {404: {"model": schemas.ErrorResponse}, 503: {"model": schemas.ErrorResponse}}

    @app.get("/health", response_model=schemas.Health, tags=["ops"])
    async def health(request: Request):
        # never calls the portal, this should stay quick even if it's down
        store: Store = request.app.state.store
        count = store.loaded_meter_count
        meta = _meta(store)
        return {
            "status": "degraded" if store.stale else ("ok" if count else "starting"),
            "meters_loaded": count,
            "as_of": meta["as_of"],
            "stale": meta["stale"],
        }

    @app.post("/v1/refresh", response_model=schemas.Health, tags=["ops"])
    async def refresh(request: Request):
        """Pull the export again right now rather than waiting for the TTL."""
        store: Store = request.app.state.store
        index = await store.index(force=True)
        meta = _meta(store)
        return {
            "status": "degraded" if store.stale else "ok",
            "meters_loaded": len(index.meters),
            "as_of": meta["as_of"],
            "stale": meta["stale"],
        }

    @app.get("/v1/meters", response_model=schemas.MeterList, tags=["meters"])
    async def list_meters(
        request: Request,
        q: Annotated[str | None, Query(description="Part of a meter ID or serial")] = None,
        make: str | None = None,
        phase_type: str | None = None,
        install_status: str | None = None,
        install_type: str | None = None,
        zone: str | None = None,
        circle: str | None = None,
        division: str | None = None,
        subdivision: str | None = None,
        substation: str | None = None,
        feeder: str | None = None,
        dt: str | None = None,
        lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
        lng: Annotated[float | None, Query(ge=-180, le=180)] = None,
        radius_km: Annotated[float | None, Query(gt=0)] = None,
        page: PageParam = 1,
        page_size: PageSizeParam = 50,
    ):
        """Filter on anything. All filters are ANDed, codes are exact and
        case-insensitive. With lat/lng/radius_km you get nearest first."""
        geo = [lat, lng, radius_km]
        if any(v is not None for v in geo) and any(v is None for v in geo):
            raise HTTPException(400, "lat, lng and radius_km have to be sent together")

        store: Store = request.app.state.store
        index = await store.index()
        needle = q.lower() if q else None
        levels = {
            "zone": zone, "circle": circle, "division": division, "subdivision": subdivision,
            "substation": substation, "feeder": feeder, "dt": dt,
        }

        found = []
        for meter in index.meters.values():
            if needle and needle not in meter["meter_id"].lower() \
                    and needle not in meter["serial_no"].lower():
                continue
            if not (
                _matches(meter, make, "make")
                and _matches(meter, phase_type, "phase_type")
                and _matches(meter, install_status, "install_status")
                and _matches(meter, install_type, "install_type")
            ):
                continue
            if not all(
                value is None or (meter["network"][level]["code"] or "").lower() == value.lower()
                for level, value in levels.items()
            ):
                continue
            if radius_km is not None:
                away = distance_km(lat, lng, meter["location"]["lat"], meter["location"]["lng"])
                if away > radius_km:
                    continue
                meter = {**meter, "distance_km": round(away, 3)}
            found.append(meter)

        if radius_km is not None:
            found.sort(key=lambda m: m["distance_km"])

        start = (page - 1) * page_size
        return {
            "data": found[start:start + page_size],
            "meta": _meta(store, total=len(found), page=page, page_size=page_size),
        }

    async def _known_meter(request: Request, meter_id: str) -> tuple[Store, dict]:
        # checking our index first saves a portal call on every typo
        store: Store = request.app.state.store
        index = await store.index()
        meter = index.meters.get(meter_id.upper())
        if meter is None:
            raise HTTPException(404, f"No meter called {meter_id}")
        return store, meter

    @app.get(
        "/v1/meters/{meter_id}", response_model=schemas.MeterResponse,
        responses=errors, tags=["meters"],
    )
    async def get_meter(request: Request, meter_id: str):
        store, meter = await _known_meter(request, meter_id)
        return {"data": meter, "meta": _meta(store)}

    def _check_dates(start: date | None, end: date | None) -> None:
        # the portal quietly ignores bad dates and returns the default window,
        # which is a nasty way to get wrong data. So we're strict here.
        if (start is None) != (end is None):
            raise HTTPException(400, "Send both from and to, or neither")
        if start and start > end:
            raise HTTPException(400, "from can't be after to")

    @app.get(
        "/v1/meters/{meter_id}/readings", response_model=schemas.ReadingList,
        responses=errors, tags=["meters"],
    )
    async def get_readings(
        request: Request,
        meter_id: str,
        start: Annotated[date | None, Query(alias="from", description="YYYY-MM-DD")] = None,
        end: Annotated[date | None, Query(alias="to", description="YYYY-MM-DD, inclusive")] = None,
    ):
        """Raw readings. With no dates you get the portal's default, the last
        7 days it has data for, which isn't necessarily the last 7 days."""
        _check_dates(start, end)
        store, meter = await _known_meter(request, meter_id)
        raw = await store.readings(
            meter["meter_id"],
            start.isoformat() if start else None,
            end.isoformat() if end else None,
        )
        rows = clean_readings(raw)
        return {"data": rows, "meta": _meta(store, total=len(rows))}

    @app.get(
        "/v1/meters/{meter_id}/consumption", response_model=schemas.ConsumptionList,
        responses=errors, tags=["meters"],
    )
    async def get_consumption(
        request: Request,
        meter_id: str,
        start: Annotated[date, Query(alias="from", description="YYYY-MM-DD")],
        end: Annotated[date, Query(alias="to", description="YYYY-MM-DD, inclusive")],
    ):
        """kWh per day. Works for half-hourly and daily meters."""
        _check_dates(start, end)
        store, meter = await _known_meter(request, meter_id)
        # one extra day, otherwise the first reading has nothing to subtract from
        raw = await store.readings(
            meter["meter_id"], (start - timedelta(days=1)).isoformat(), end.isoformat()
        )
        days = daily_consumption(clean_readings(raw), start.isoformat())
        return {"data": days, "meta": _meta(store, total=len(days))}

    @app.get("/v1/transformers", response_model=schemas.TransformerList, tags=["network"])
    async def list_transformers(request: Request):
        store: Store = request.app.state.store
        index = await store.index()
        items = list(index.transformers.values())
        return {"data": items, "meta": _meta(store, total=len(items))}

    @app.get(
        "/v1/transformers/{code}", response_model=schemas.TransformerResponse,
        responses=errors, tags=["network"],
    )
    async def get_transformer(request: Request, code: str):
        store: Store = request.app.state.store
        index = await store.index()
        item = index.transformers.get(code.upper())
        if item is None:
            raise HTTPException(404, f"No transformer called {code}")
        return {"data": item, "meta": _meta(store)}

    @app.get("/v1/network/tree", response_model=schemas.Tree, tags=["network"])
    async def network_tree(request: Request):
        """Zone down to DT, with meter counts. Read the README first, some codes
        show up under more than one parent and that's in the source data."""
        store: Store = request.app.state.store
        index = await store.index()
        return {"data": build_tree(index.meters.values()), "meta": _meta(store)}

    @app.get("/v1/network/issues", response_model=schemas.IssueList, tags=["network"])
    async def network_issues(request: Request):
        """Everything we fixed or couldn't fix in the portal's hierarchy."""
        store: Store = request.app.state.store
        index = await store.index()
        return {"data": index.issues, "meta": _meta(store, total=len(index.issues))}


app = create_app()
