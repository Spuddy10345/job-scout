"""Job Scout: FastAPI app + in-process scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler
from .auth import SecurityMiddleware, session_secret
from .credentials import env, install_log_redaction
from .db import init_db
from .web import extras
from .web.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
install_log_redaction()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    run_scheduler = env("JOBSCOUT_DISABLE_SCHEDULER") not in ("1", "true")  # off for demos and UI work
    if run_scheduler:
        scheduler.start()
    yield
    if run_scheduler:
        scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="Job Scout", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityMiddleware)
# added last so it runs first: the security middleware reads the session
app.add_middleware(SessionMiddleware, secret_key=session_secret(), session_cookie="jobscout_session",
                   max_age=30 * 86400, same_site="lax", https_only=env("JOBSCOUT_HTTPS_ONLY") == "1")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
app.include_router(router)
app.include_router(extras.router)
