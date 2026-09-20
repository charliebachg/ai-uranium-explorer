"""Paced ArcGIS REST client with a raw-page cache.

Observed behaviour of the Saskatchewan services (2026-09-17):
- GeoDS (geoscience-data-system.saskatchewan.ca) sits behind a WAF that sometimes answers HTTP 200 with an
  HTML "Unauthorized Request Blocked" page, non-deterministically, mostly for compound where-clauses.
  So: single-clause filters, filter locally, assert JSON, retry with backoff, pace requests.
- gis.saskatchewan.ca is well behaved; pacing is still polite.
- Pagination works with resultOffset/resultRecordCount up to each layer's maxRecordCount.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from .ids import sha256_json

USER_AGENT = "ai-uranium-explorer/0.1 (research demo; public data only)"


class ArcGisError(Exception):
    pass


class WafBlocked(ArcGisError):
    """The WAF returned HTML instead of JSON after all retries."""


@dataclass
class ArcGisClient:
    cache_dir: Path
    min_interval_s: dict[str, float] = field(default_factory=lambda: {
        "geoscience-data-system.saskatchewan.ca": 2.5,
        "gis.saskatchewan.ca": 0.6,
        "maps-cartes.services.geo.ca": 0.6,
    })
    backoff_s: tuple[float, ...] = (2, 5, 15, 45, 120)
    timeout_s: float = 90.0
    _last: dict[str, float] = field(default_factory=dict)
    _http: httpx.Client | None = None

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._http = httpx.Client(timeout=self.timeout_s, headers={"User-Agent": USER_AGENT},
                                  follow_redirects=True)

    def close(self) -> None:
        if self._http:
            self._http.close()

    # ---------- low level ----------

    def _pace(self, host: str) -> None:
        gap = self.min_interval_s.get(host, 1.0)
        wait = self._last.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def get_json(self, url: str, params: dict[str, Any], use_cache: bool = True) -> dict[str, Any]:
        key = sha256_json({"url": url, "params": params})
        path = self.cache_dir / key[:2] / f"{key}.json"
        if use_cache and path.is_file():
            return json.loads(path.read_text())
        host = urlparse(url).hostname or ""
        last_err: Exception | None = None
        for attempt, delay in enumerate((0.0, *self.backoff_s)):
            if delay:
                time.sleep(delay * (0.8 + 0.4 * random.random()))
            self._pace(host)
            try:
                assert self._http is not None
                r = self._http.get(url, params=params)
            except httpx.HTTPError as e:
                last_err = e
                continue
            ctype = r.headers.get("content-type", "")
            body = r.text
            if r.status_code >= 500 or r.status_code == 429:
                last_err = ArcGisError(f"HTTP {r.status_code}")
                continue
            if "json" not in ctype and not body.lstrip().startswith("{"):
                blocked = "Unauthorized Request Blocked" in body or "<html" in body[:500].lower()
                last_err = WafBlocked(f"non-JSON response ({ctype}) attempt {attempt + 1}") if blocked else \
                    ArcGisError(f"unexpected content-type {ctype}")
                continue
            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                last_err = e
                continue
            if isinstance(data, dict) and "error" in data:
                last_err = ArcGisError(f"ArcGIS error: {data['error']}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps(data))
            tmp.rename(path)
            return data
        if isinstance(last_err, WafBlocked):
            raise last_err
        raise ArcGisError(f"{url} failed after retries: {last_err}")

    # ---------- layer helpers ----------

    def layer_info(self, layer_url: str) -> dict[str, Any]:
        return self.get_json(layer_url, {"f": "json"})

    def count(self, layer_url: str, where: str = "1=1") -> int:
        data = self.get_json(f"{layer_url}/query", {"where": where, "returnCountOnly": "true", "f": "json"})
        return int(data["count"])

    def iter_pages(self, layer_url: str, where: str = "1=1", out_fields: str = "*", geometry: bool = True,
                   out_sr: int | None = 4326, page_size: int | None = None, fmt: str = "json",
                   order_by: str | None = None, extra: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        info = self.layer_info(layer_url)
        max_rc = int(info.get("maxRecordCount") or 1000)
        size = min(page_size or max_rc, max_rc)
        oid = info.get("objectIdField") or next(
            (f["name"] for f in info.get("fields", []) if f.get("type") == "esriFieldTypeOID"), "OBJECTID")
        offset = 0
        while True:
            params: dict[str, Any] = {
                "where": where, "outFields": out_fields, "returnGeometry": "true" if geometry else "false",
                "resultOffset": offset, "resultRecordCount": size, "orderByFields": order_by or oid, "f": fmt,
            }
            if geometry and out_sr:
                params["outSR"] = out_sr
            if extra:
                params.update(extra)
            page = self.get_json(f"{layer_url}/query", params)
            feats = page.get("features", [])
            yield page
            if not feats:
                break
            offset += len(feats)
            exceeded = page.get("exceededTransferLimit") or page.get("properties", {}).get("exceededTransferLimit")
            if len(feats) < size and not exceeded:
                break

    def fetch_all(self, layer_url: str, **kw: Any) -> list[dict[str, Any]]:
        feats: list[dict[str, Any]] = []
        for page in self.iter_pages(layer_url, **kw):
            feats.extend(page.get("features", []))
        return feats
