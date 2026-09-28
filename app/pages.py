"""Fetch a web page as readable text: plain HTTP first (free), Firecrawl markdown when a page
needs a real browser (JS-rendered careers sites, bot-protected boards). Firecrawl credits are
metered against a daily budget.
"""

from __future__ import annotations

import logging
import threading

import httpx
from selectolax.parser import HTMLParser

from .config import AppSettings, env, get_kv, set_kv
from .db import utc_today
from .net import http_client

log = logging.getLogger("jobscout.pages")

FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v2/scrape"
_credit_lock = threading.Lock()


class CreditsExceeded(RuntimeError):
    pass


def firecrawl_available() -> bool:
    return bool(env("FIRECRAWL_API_KEY"))


def credits_today() -> int:
    return get_kv("firecrawl_usage", {}).get(utc_today(), 0)


def _charge(settings: AppSettings, credits: int) -> None:
    with _credit_lock:
        today = utc_today()
        used = get_kv("firecrawl_usage", {}).get(today, 0)
        if used + credits > settings.llm.firecrawl_daily_credits:
            raise CreditsExceeded(f"Firecrawl daily budget reached ({used}/{settings.llm.firecrawl_daily_credits})")
        set_kv("firecrawl_usage", {today: used + credits})


def html_to_text(html: str) -> str:
    """Readable text that keeps link targets, so the extractor can recover per-job URLs."""
    tree = HTMLParser(html)
    for sel in ("script", "style", "noscript", "svg", "header nav", "footer"):
        for n in tree.css(sel):
            n.decompose()
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        label = a.text(strip=True)
        if label and href and not href.startswith(("#", "javascript:", "mailto:")):
            a.replace_with(f"[{label}]({href})")
    body = tree.body or tree.root
    text = body.text(separator="\n", strip=True) if body else ""
    lines = [ln for ln in (l.strip() for l in text.splitlines()) if ln]
    return "\n".join(lines)


def firecrawl_markdown(settings: AppSettings, url: str) -> str:
    _charge(settings, 1)
    r = httpx.post(
        FIRECRAWL_SCRAPE,
        headers={"Authorization": f"Bearer {env('FIRECRAWL_API_KEY')}"},
        json={"url": url, "formats": ["markdown"], "onlyMainContent": True, "maxAge": 3_600_000},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("success", True):
        raise RuntimeError(f"firecrawl: {data.get('error')}")
    return (data.get("data") or {}).get("markdown", "")


def plain_text(url: str, timeout: float = 15) -> str:
    """Free, best-effort fetch - empty string if the page is blocked or JS-only."""
    try:
        with http_client(timeout=timeout) as c:
            r = c.get(url)
        if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
            return html_to_text(r.text)
    except httpx.HTTPError:
        pass
    return ""


def get_page_text(settings: AppSettings, url: str, render: bool = False, min_chars: int = 800) -> tuple[str, str]:
    """Return (text, via). render=True goes straight to Firecrawl."""
    if not render:
        try:
            with http_client() as c:
                r = c.get(url)
            if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
                text = html_to_text(r.text)
                if len(text) >= min_chars:
                    return text, "http"
        except httpx.HTTPError as e:
            log.info("plain fetch failed for %s: %s", url, e)
    if firecrawl_available():
        return firecrawl_markdown(settings, url), "firecrawl"
    return "", "none"
