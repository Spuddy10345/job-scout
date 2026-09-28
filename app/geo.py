"""Geocoding (postcodes.io, free, no key) with a SQLite cache, and distance to search centres."""

from __future__ import annotations

import logging
import math
import re
import threading
import time

import httpx
from sqlalchemy.exc import OperationalError

from .config import Centre
from .credentials import env
from .db import GeoCache, session

log = logging.getLogger("jobscout.geo")

API = "https://api.postcodes.io"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent with a way to reach you.
NOMINATIM_UA = f"job-scout/0.2 (self-hosted job search; {env('JOBSCOUT_CONTACT') or 'https://github.com/Spuddy10345/job-scout'})"
_nominatim_lock = threading.Lock()
_nominatim_last = 0.0
POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
OUTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b")
REMOTE = re.compile(r"\b(fully[- ]remote|remote|work from home|home[- ]based|wfh|anywhere in the uk)\b", re.I)
NOISE = re.compile(
    r"\b(hybrid|remote|on-?site|office|based|uk|united kingdom|gb|england|wales|area|city of)\b|\(.*?\)|[^\w\s,'-]", re.I
)
# Too vague to place - keep such jobs rather than guess a centroid.
REGIONS = {"south wales", "wales", "south west", "south west england", "uk", "united kingdom", "england", "gb",
           "great britain", "multiple locations", "various locations", "nationwide", "various", "uk wide", "anywhere"}
TYPE_RANK = {"City": 0, "Town": 1, "Suburban Area": 2, "Village": 3, "Hamlet": 4, "Other Settlement": 5}


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest(lat: float, lon: float, centres: list[Centre]) -> tuple[Centre | None, float | None]:
    best, best_d = None, None
    for c in centres:
        d = haversine_mi(lat, lon, c.lat, c.lon)
        if best_d is None or d < best_d:
            best, best_d = c, d
    return best, best_d


def is_remote_text(*parts: str) -> bool:
    return any(REMOTE.search(p or "") for p in parts)


def _lookup(location: str, centres: list[Centre]) -> tuple[float, float] | None:
    if " ".join(re.sub(r"[^a-z ]+", " ", location.lower()).split()) in REGIONS:
        return None
    with httpx.Client(timeout=15) as c:
        if m := POSTCODE.search(location):
            r = c.get(f"{API}/postcodes/{m.group(1)}{m.group(2)}")
            if r.status_code == 200:
                res = r.json()["result"]
                return res["latitude"], res["longitude"]
        parts = [p.strip() for p in location.split(",") if p.strip()]
        head = NOISE.sub(" ", parts[0] if parts else location).strip(" -,")
        head = re.sub(r"\s+", " ", head)
        hints = [p.lower() for p in parts[1:]]
        if m := OUTCODE.fullmatch(head.upper()):
            r = c.get(f"{API}/outcodes/{m.group(1)}")
            if r.status_code == 200:
                res = r.json()["result"]
                return res["latitude"], res["longitude"]
        if not head:
            return None
        # "Cardiff" first, then each token of formats like "GBR-Wales-Newport-Celtic Lakes-KLA"
        tokens = [head] + [t.strip() for t in re.split(r"[,;/|–]|\s-\s|-(?=[A-Z])", location) if t.strip()]
        for token in dict.fromkeys(t for t in tokens if len(t) > 2 and t.lower() not in REGIONS):
            if coords := _town(c, token, hints, centres):
                return coords
        # OS Open Names indexes Welsh places by their Welsh name (Casnewydd, not Newport) and only
        # covers Great Britain - OpenStreetMap, biased toward the search area, handles the rest.
        return _nominatim(c, ", ".join([head, *parts[1:]]), centres)


def _town(c: httpx.Client, name: str, hints: list[str], centres: list[Centre]) -> tuple[float, float] | None:
    """A GB city/town called `name` (matching English or Welsh name), preferring hinted/nearby ones."""
    r = c.get(f"{API}/places", params={"q": name, "limit": 50})
    results = r.json().get("result") or [] if r.status_code == 200 else []
    name = name.lower()
    towns = [x for x in results if x.get("latitude") is not None and x.get("local_type") in ("City", "Town")
             and name in ((x.get("name_1") or "").lower(), (x.get("name_2") or "").lower())]
    if not towns:
        return None

    def rank(x):
        text = " ".join(str(x.get(k) or "") for k in ("county_unitary", "district_borough", "region", "country")).lower()
        hinted = 0 if any(h and h in text for h in hints) else 1
        _, d = nearest(x["latitude"], x["longitude"], centres)
        return (hinted, TYPE_RANK[x["local_type"]], d if d is not None else 1e9)

    best = min(towns, key=rank)
    return best["latitude"], best["longitude"]


def _nominatim(c: httpx.Client, query: str, centres: list[Centre]) -> tuple[float, float] | None:
    global _nominatim_last
    query = NOISE.sub(" ", query).strip(" ,-")
    if not query:
        return None
    with _nominatim_lock:  # usage policy: max 1 request/second
        wait = 1.1 - (time.monotonic() - _nominatim_last)
        if wait > 0:
            time.sleep(wait)
        params = {"q": query, "format": "jsonv2", "limit": 1}
        if centres:  # prefer (not restrict to) results around the search centres
            lats, lons = [x.lat for x in centres], [x.lon for x in centres]
            params.update(viewbox=f"{min(lons) - 1},{max(lats) + 1},{max(lons) + 1},{min(lats) - 1}", bounded=0)
        r = c.get(NOMINATIM, params=params, headers={"User-Agent": NOMINATIM_UA})
        _nominatim_last = time.monotonic()
    if r.status_code != 200 or not r.json():
        return None
    hit = r.json()[0]
    return float(hit["lat"]), float(hit["lon"])


def geocode(location: str, centres: list[Centre]) -> tuple[float, float] | None:
    key = (location or "").strip().lower()
    if not key:
        return None
    with session() as s:
        hit = s.get(GeoCache, key)
        if hit is not None:
            return (hit.lat, hit.lon) if hit.lat is not None else None
    try:
        coords = _lookup(location, centres)
    except httpx.HTTPError as e:
        log.info("geocode failed for %r: %s", location, e)
        return None  # transient - don't cache
    try:
        with session() as s:
            s.merge(GeoCache(query=key, lat=coords[0] if coords else None, lon=coords[1] if coords else None))
            s.commit()
    except OperationalError as e:  # cache is best-effort; never block the caller on a busy database
        log.info("geocache write skipped for %r: %s", location, e)
    return coords


def warm(locations: set[str], centres: list[Centre]) -> None:
    """Geocode (and cache) locations up front, outside any write transaction."""
    for loc in sorted(x for x in locations if x and x.strip()):
        geocode(loc, centres)
