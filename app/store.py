"""Caching. Holds the index and recent readings so we go easy on the portal."""

import asyncio
import logging
import time
from collections import OrderedDict

from .network import Index, build_index
from .portal import PortalClient, PortalError

log = logging.getLogger(__name__)


class Store:
    def __init__(self, portal: PortalClient, index_ttl: float, readings_ttl: float):
        self._portal = portal
        self._index_ttl = index_ttl
        self._readings_ttl = readings_ttl
        self._index: Index | None = None
        self._index_loaded_at = 0.0  # epoch time, shown to API users
        self._index_lock = asyncio.Lock()
        self.stale = False
        self._readings: OrderedDict[tuple, tuple[float, list]] = OrderedDict()

    @property
    def loaded_meter_count(self) -> int:
        return len(self._index.meters) if self._index else 0

    @property
    def as_of(self) -> float:
        return self._index_loaded_at

    async def index(self, force: bool = False) -> Index:
        if not force and self._fresh():
            return self._index
        # the lock stops ten requests all hitting the export at once
        async with self._index_lock:
            if not force and self._fresh():
                return self._index
            try:
                raw_meters = await self._portal.export_meters()
                raw_transformers = await self._portal.list_transformers()
                self._index = build_index(raw_meters, raw_transformers)
                self._index_loaded_at = time.time()
                self.stale = False
                log.info("index refreshed, %d meters", len(self._index.meters))
            except PortalError:
                # old data with a stale flag beats a 503 when the portal wobbles
                if self._index is None:
                    raise
                log.exception("refresh failed, serving stale index")
                self.stale = True
                # don't hammer a struggling portal on every incoming request
                self._index_loaded_at = time.time() - self._index_ttl + 60
            return self._index

    def _fresh(self) -> bool:
        return self._index is not None and time.time() - self._index_loaded_at < self._index_ttl

    async def readings(self, meter_id: str, start: str | None, end: str | None) -> list[dict]:
        key = (meter_id, start, end)
        cached = self._readings.get(key)
        if cached and time.time() - cached[0] < self._readings_ttl:
            self._readings.move_to_end(key)
            return cached[1]
        rows = await self._portal.readings(meter_id, start, end)
        self._readings[key] = (time.time(), rows)
        # a month of readings is ~100KB, so cap it instead of growing forever
        while len(self._readings) > 500:
            self._readings.popitem(last=False)
        return rows
