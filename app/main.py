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
from app.routers import auth, cron, defectives, inventory, users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("jstreturn")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_pool()
        log.info("DB pool ready")
    except Exception as e:
        log.exception("DB pool init failed; service will keep running but /healthz will report DB down")
    yield
    try:
        await close_pool()
    except Exception:
        pass
    log.info("DB pool closed")


app = FastAPI(
    title="jstreturn",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(defectives.router)
app.include_router(inventory.router)
app.include_router(cron.router)


@app.get("/healthz")
async def healthz():
    try:
        from app.db import db_url_status, pool
        info = db_url_status()
        if not info["set"]:
            return JSONResponse(
                {"ok": False, "error": "DATABASE_URL not set", "info": info},
                status_code=503,
            )
        async with pool().acquire() as conn:
            v = await conn.fetchval("SELECT 1")
        return {"ok": True, "db": v == 1, "info": info}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"version": "0.1.0"},
        )
    except Exception as e:
        log.exception("template render failed")
        return JSONResponse(
            {"error": "template_render_failed", "detail": str(e)}, status_code=500
        )