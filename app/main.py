"""FastAPI app entry."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import close_pool, init_pool
from app.routers import auth, cron, defectives, imports, inventory, users

VERSION = "0.1.0"
# 1x1 transparent PNG, inlined so browsers don't 404 on /favicon.ico.
FAVICON_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364600000000600023081d02f0000000049454e44ae42"
    "6082"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("jstreturn")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db import db_url_status
    info = db_url_status()
    log.info("starting jstreturn v%s; DATABASE_URL set=%s host=%s", VERSION, info["set"], info["host"])
    try:
        await init_pool()
        log.info("DB pool ready (host=%s ssl=%s)", info["host"], info["has_ssl"])
    except Exception:
        log.exception("DB pool init failed; service will keep running but /healthz will report DB down")
    yield
    try:
        await close_pool()
    except Exception:
        log.exception("DB pool close failed")
    log.info("jstreturn stopped")


app = FastAPI(
    title="jstreturn",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(defectives.router)
app.include_router(imports.router)
app.include_router(inventory.router)
app.include_router(cron.router)


@app.get("/healthz")
async def healthz():
    """Liveness + DB readiness. Includes version for deploy verification."""
    try:
        from app.db import db_url_status, pool
        info = db_url_status()
        body = {"ok": True, "db": False, "version": VERSION, "info": info}
        if not info["set"]:
            body["ok"] = False
            body["error"] = "DATABASE_URL not set"
            return JSONResponse(body, status_code=503)
        async with pool().acquire() as conn:
            v = await conn.fetchval("SELECT 1")
        body["db"] = v == 1
        return body
    except Exception as e:
        return JSONResponse(
            {"ok": False, "db": False, "version": VERSION, "error": str(e)},
            status_code=503,
        )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Return inline PNG so browsers don't 404 and trigger log noise."""
    return Response(content=FAVICON_PNG, media_type="image/png")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve the standalone login page (no modal, no app)."""
    try:
        return templates.TemplateResponse(request, "login.html", {})
    except Exception as e:
        log.exception("login template render failed")
        return JSONResponse(
            {"error": "template_render_failed", "detail": str(e)}, status_code=500
        )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"version": VERSION},
        )
    except Exception as e:
        log.exception("template render failed")
        return JSONResponse(
            {"error": "template_render_failed", "detail": str(e)}, status_code=500
        )
