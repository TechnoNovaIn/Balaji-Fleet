import time
from urllib.parse import quote

import httpx

from app.config import settings


class IntanglesAuthError(RuntimeError):
    pass


class IntanglesClient:
    def __init__(self, base_url: str | None = None, username: str | None = None, password: str | None = None):
        self.base_url = base_url or settings.intangles_base_url
        self.username = username or settings.intangles_username
        self.password = password or settings.intangles_password
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def login(self) -> str:
        url = (
            f"{self.base_url}/WebLogin"
            f"?Username={quote(self.username)}&password={quote(self.password)}"
        )
        resp = httpx.post(url, headers={"accept": "*/*"}, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise IntanglesAuthError(payload.get("message", "login failed"))
        self._token = payload["token"]
        self._token_expiry = time.time() + 60 * 55
        return self._token

    def token(self) -> str:
        if not self._token or time.time() >= self._token_expiry:
            self.login()
        return self._token

    def get_vehicles(self) -> list[dict]:
        data = self._get("/VehicleHealth/GetIntangleVehicles", headers={"accept": "text/plain"})
        return [{"intangle_id": int(v["IntangleId"]), "vehicle_no": v["Vehicle"]} for v in data]

    def get_obd(self, vehicle_nos: list[str], from_dt: str, to_dt: str) -> list[dict]:
        params = {
            "vehicleNos": ",".join(vehicle_nos),
            "fromDate": from_dt,
            "toDate": to_dt,
        }
        return self._get("/IntangleVehicleReports/GetVehicleOBD", params=params)

    def get_gps(self, vehicle_nos: list[str], from_date: str, to_date: str) -> list[dict]:
        params = {
            "vehicleNos": ",".join(vehicle_nos),
            "fromDate": from_date,
            "toDate": to_date,
        }
        return self._get("/IntangleVehicleReports/GetIntanglesVehicleLocation", params=params)

    def _get(self, path: str, params: dict | None = None, headers: dict | None = None) -> list:
        try:
            return self._request(path, params, headers)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self._token = None
                self.login()
                return self._request(path, params, headers)
            raise

    def _request(self, path: str, params: dict | None = None, headers: dict | None = None, _attempt: int = 0) -> list:
        h = {"accept": "*/*", "Authorization": f"Bearer {self.token()}"}
        if headers:
            h.update(headers)
        try:
            # 90s read timeout: a stalled Intangles connection (very common) must
            # fail fast and be caught by the batch-level TransportError handler
            # so the backfill moves on instead of hanging for many minutes
            # (previously 300s * retries could block a single batch ~20 min).
            resp = httpx.get(f"{self.base_url}{path}", params=params, headers=h, timeout=90)
        except httpx.TransportError:
            # Connection resets, timeouts, DNS failures etc. are transient on the
            # Intangles feed; retry like the 5xx path instead of killing the caller.
            if _attempt < 3:
                time.sleep(2 * (_attempt + 1))
                return self._request(path, params, headers, _attempt + 1)
            raise
        if resp.status_code >= 500 and _attempt < 3:
            time.sleep(2 * (_attempt + 1))
            return self._request(path, params, headers, _attempt + 1)
        resp.raise_for_status()
        return resp.json()
