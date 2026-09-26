"""Job Scout: FastAPI app + in-process scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .db import init_db
from .web.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="Job Scout", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
app.include_router(router)
