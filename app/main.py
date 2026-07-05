"""FastAPI app entry."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import close_pool, init_pool
from app.routers import auth, cron, defectives, inventory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("jstreturn")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    log.info("DB pool ready")
    yield
    await close_pool()
    log.info("DB pool closed")


app = FastAPI(
    title="jstreturn",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.include_router(auth.router)
app.include_router(defectives.router)
app.include_router(inventory.router)
app.include_router(cron.router)


@app.get("/healthz")
async def healthz():
    try:
        from app.db import pool
        async with pool().acquire() as conn:
            v = await conn.fetchval("SELECT 1")
        return {"ok": True, "db": v == 1}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "version": "0.1.0"},
    )