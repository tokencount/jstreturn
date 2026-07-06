"""asyncpg connection pool + idempotent startup schema bootstrap.

The deployed service auto-creates any missing tables on first run. This is
idempotent — safe to run on every startup and on a fresh database.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import parse_qs, urlparse

import asyncpg

log = logging.getLogger("jstreturn")

_pool: asyncpg.Pool | None = None


# Idempotent schema for any tables that may be missing.
# Mirrors docs/schema.sql but does NOT depend on pg_dump-specific SQL.
SCHEMA_SQL = """
CREATE TYPE IF NOT EXISTS public.defective_status AS ENUM ('PENDING', 'READY', 'COMPLETED');

CREATE TABLE IF NOT EXISTS public.users (
    id           SERIAL PRIMARY KEY,
    telegram_id  BIGINT UNIQUE,
    name         TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('returns','repair','admin')),
    active       BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.defective_items (
    id             SERIAL PRIMARY KEY,
    pallet_no      TEXT NOT NULL,
    product_name   TEXT,
    sku            TEXT NOT NULL,
    qty            INTEGER NOT NULL CHECK (qty > 0),
    status         public.defective_status DEFAULT 'PENDING'::public.defective_status,
    created_by     INTEGER REFERENCES public.users(id),
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    completed_by   INTEGER REFERENCES public.users(id),
    completed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_def_pallet  ON public.defective_items (pallet_no);
CREATE INDEX IF NOT EXISTS idx_def_sku     ON public.defective_items (sku);
CREATE INDEX IF NOT EXISTS idx_def_status  ON public.defective_items (status);

CREATE TABLE IF NOT EXISTS public.defective_parts (
    id             SERIAL PRIMARY KEY,
    defective_id   INTEGER REFERENCES public.defective_items(id) ON DELETE CASCADE,
    part_code      TEXT NOT NULL,
    part_name      TEXT,
    qty            INTEGER NOT NULL CHECK (qty > 0)
);
CREATE INDEX IF NOT EXISTS idx_dp_defective ON public.defective_parts (defective_id);
CREATE INDEX IF NOT EXISTS idx_dp_part      ON public.defective_parts (part_code);

CREATE TABLE IF NOT EXISTS public.inventory_snapshot (
    part_code          TEXT PRIMARY KEY,
    part_name          TEXT,
    on_hand_qty        INTEGER NOT NULL DEFAULT 0,
    location           TEXT,
    source_updated_at  TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.audit_log (
    id           BIGSERIAL PRIMARY KEY,
    user_id      INTEGER REFERENCES public.users(id),
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    INTEGER,
    details      JSONB,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON public.audit_log (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_time   ON public.audit_log (created_at DESC);
"""


def _parse_dsn():
    """asyncpg doesn't understand ?ssl=true. Convert to ssl=True."""
    dsn = os.environ["DATABASE_URL"]
    u = urlparse(dsn)
    qs = parse_qs(u.query)
    ssl_required = qs.get("ssl", ["false"])[0].lower() in ("true", "1", "require")
    new_query = "&".join(
        f"{k}={v[0]}" for k, v in qs.items() if k.lower() != "ssl"
    )
    new_dsn = f"{u.scheme}://{u.netloc}{u.path}"
    if new_query:
        new_dsn += "?" + new_query
    return new_dsn, ssl_required


async def init_pool() -> asyncpg.Pool:
    """Open the connection pool and run idempotent schema bootstrap."""
    global _pool
    if _pool is None:
        dsn, ssl_required = _parse_dsn()
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=5,
            command_timeout=30,
            ssl=ssl_required,
        )
        # Apply any missing schema.
        async with _pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
            log.info("schema bootstrap applied (idempotent CREATE … IF NOT EXISTS)")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    return _pool


def db_url_status() -> dict:
    """Debug helper: return info about DATABASE_URL for diagnostics."""
    dsn = os.environ.get("DATABASE_URL", "")
    return {
        "set": bool(dsn),
        "len": len(dsn),
        "prefix": dsn[:30] + "..." if len(dsn) > 30 else dsn,
        "has_ssl": "ssl=" in dsn,
        "host": (dsn.split("@")[-1].split("/")[0] if "@" in dsn else "?"),
    }
