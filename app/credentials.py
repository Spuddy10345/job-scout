"""Secrets live in the environment only - never in settings, the database or templates.

Each secret can be given directly (``BRAVE_API_KEY=...``) or as a file path for Docker/compose
secrets (``BRAVE_API_KEY_FILE=/run/secrets/brave``). ``redact()`` masks them in anything the app
stores, logs or shows, since HTTP errors happily echo request URLs (Adzuna puts its key in the
query string).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Loaded here, before anything reads the environment. Real variables win over .env; tests opt out.
if not os.environ.get("JOBSCOUT_NO_DOTENV"):
    load_dotenv(ROOT / ".env")

SECRET_NAMES = (
    "ANTHROPIC_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "REED_API_KEY", "BRAVE_API_KEY",
    "FIRECRAWL_API_KEY", "HA_TOKEN", "APPRISE_URLS", "JOBSCOUT_PASSWORD", "JOBSCOUT_SECRET_KEY",
)
QUERY_SECRET = re.compile(
    r"(?i)\b(app_key|app_id|api_?key|apikey|key|token|access_token|secret|password)=([^&\s'\"]+)"
)
BEARER = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{12,}")


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return ""
    return ""


def _secret_values() -> list[str]:
    values = []
    for name in SECRET_NAMES:
        v = env(name)
        if not v:
            continue
        values.append(v)
        if name == "APPRISE_URLS":
            values += [u for u in re.split(r"[\s,]+", v) if u]
    # longest first so a value containing another is masked whole
    return sorted((v for v in values if len(v) >= 6), key=len, reverse=True)


def redact(text: str) -> str:
    if not text:
        return text
    for value in _secret_values():
        text = text.replace(value, "***")
    text = QUERY_SECRET.sub(r"\1=***", text)
    return BEARER.sub(r"\1 ***", text)


class RedactingFormatter(logging.Formatter):
    """Masks secrets in the fully formatted record, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def install_log_redaction() -> None:
    for handler in logging.getLogger().handlers:
        fmt = handler.formatter._fmt if handler.formatter else None  # noqa: SLF001
        handler.setFormatter(RedactingFormatter(fmt))
