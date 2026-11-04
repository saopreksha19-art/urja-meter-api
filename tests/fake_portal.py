"""A tiny stand-in for the real portal, so tests run offline and don't use up
its rate limit. Response shapes and sample rows are copied from the real thing,
including the broken ones. Failures can be switched on per test.
"""

import hashlib
import hmac
import time

import httpx

SECRET = "not-the-real-secret"


def _node(code, name):
    return {"code": code, "name": name}


def _meter(meter_id, serial, make, dt_code, dt_name, circle=("C-01", "Circle 1"),
           feeder=("F-001", "Feeder 1"), division=("D-01", "Division 1"), status="Installed",
           lat=26.93, lng=75.83):
    return {
        "meterId": meter_id, "serialNo": serial, "make": make, "phaseType": "single",
        "installStatus": status, "installType": "Whole Current", "dtCode": dt_code,
        "build": "legacy", "geo": {"lat": lat, "lng": lng},
        "hierarchy": {
            "zone": _node("Z-01", "Jaipur Zone 1"), "circle": _node(*circle),
            "division": _node(*division), "subdivision": _node("SD-01", "Subdivision 1"),
            "substation": _node("SS-01", "Substation 1"), "feeder": _node(*feeder),
            "dt": _node(dt_code, dt_name),
        },
    }


METERS = [
    _meter("J100000", "SE33962", "HPL", "DT-001", "Malviya Nagar DT 1", status="Decommissioned"),
    _meter("J100001", "L&84997", "Genus", "DT-001", "Malviya Nagar DT 1", lat=27.03, lng=75.91),
    _meter("J100011", "SE48422", "HPL", "DT-001", "Malviya Nagar DT 1", circle=("", "Circle 1")),
    _meter("J100162", "GE11111", "L&T", "DT-001", "Malviya Nagar DT 1", feeder=("F-001", "")),
    _meter("J100400", "GN31592", "Genus", "DT-007", "Old Malviya Nagar Xfmr",
           circle=("C-03", "Circle 3")),
    _meter("J100500", "GE22222", "HPL", "DT-007", "Sanganer DT 7", circle=("", "")),
]

DTS = [
    {"code": "DT-001", "name": "Malviya Nagar DT 1", "feederCode": "F-001", "capacityKva": 100},
    {"code": "DT-007", "name": "Sanganer DT 7", "feederCode": "F-001", "capacityKva": 63},
]

ENERGY = [
    {"timestamp": "01/06/2026 23:30", "kwh": "100", "kvah": "110", "voltR": "230"},
    {"timestamp": "02/06/2026 00:00", "kwh": "100.5", "kvah": "110.6", "voltR": "231"},
    {"timestamp": "02/06/2026 00:30", "kwh": "", "kvah": "", "voltR": ""},
    {"timestamp": "02/06/2026 01:00", "kwh": "101.5", "kvah": "111.7", "voltR": "229"},
    {"timestamp": "03/06/2026 00:00", "kwh": "103", "kvah": "113.3", "voltR": "228"},
]


class FakePortal:
    def __init__(self):
        self.calls = []
        self.logins = 0
        self.session_valid = False
        self.secret = SECRET
        self.password = "right"
        self.fail_next = []  # status codes to return before behaving normally

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://portal.test", transport=httpx.MockTransport(self._handle)
        )

    def _json(self, status, body):
        return httpx.Response(status, json=body)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)

        if path == "/login":
            form = dict(p.split("=") for p in request.content.decode().split("&"))
            if request.headers.get("origin") != "https://portal.test":
                return httpx.Response(403, text="Cross-site POST form submissions are forbidden")
            if form["password"] != self.password:
                return httpx.Response(400, text="<p>Invalid credentials</p>")
            self.logins += 1
            self.session_valid = True
            return httpx.Response(303, headers={"location": "/meters", "set-cookie": "s=1; Path=/"})

        if not self.session_valid:
            return self._json(401, {"error": "unauthorized", "message": "A valid session is required."})

        if self.fail_next:
            status = self.fail_next.pop(0)
            if status == 429:
                return self._json(429, {"error": "rate_limited", "message": "slow down"})
            return httpx.Response(status, text="oops")

        if path == "/portal/keys":
            return self._json(200, {"data": {"signingSecret": self.secret}})

        if path == "/portal/export":
            stamp = request.headers.get("x-timestamp", "0")
            message = "\n".join(["GET", path, request.url.query.decode(), stamp])
            good = hmac.new(self.secret.encode(), message.encode(), hashlib.sha256).hexdigest()
            fresh = abs(time.time() - int(stamp)) <= 300
            if request.headers.get("x-signature") != good or not fresh:
                return self._json(401, {"error": "signature_invalid", "message": "bad"})
            return self._json(200, {"data": METERS, "total": len(METERS)})

        if path == "/portal/dts":
            page = int(request.url.params.get("page", 1))
            data = DTS[:1] if page == 1 else DTS[1:] if page == 2 else []
            return self._json(200, {"data": data, "total": len(DTS), "page": page, "pageSize": 1})

        if path.endswith("/energy"):
            if "J100000" not in path and "J100001" not in path:
                return self._json(404, {"error": "not_found", "message": "Meter not found"})
            return self._json(200, {"data": ENERGY})

        return httpx.Response(404, text="<html>not found</html>")


