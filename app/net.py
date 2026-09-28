import threading
import time

import httpx

from .credentials import env

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def http_client(**kw) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"}
    headers.update(kw.pop("headers", {}))
    return httpx.Client(headers=headers, timeout=kw.pop("timeout", 30), follow_redirects=True, **kw)


_brave_lock = threading.Lock()
_brave_last = 0.0


def brave_search(client: httpx.Client, params: dict) -> dict:
    """Brave Search, throttled across threads to BRAVE_RPS requests/second (default 1)."""
    global _brave_last
    try:
        gap = 1.05 / max(float(env("BRAVE_RPS") or 1), 0.1)
    except ValueError:
        gap = 1.05
    for attempt in range(3):
        with _brave_lock:
            wait = gap - (time.monotonic() - _brave_last)
            if wait > 0:
                time.sleep(wait)
            r = client.get("https://api.search.brave.com/res/v1/web/search", params=params)
            _brave_last = time.monotonic()
        if r.status_code == 429 and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    return {}
