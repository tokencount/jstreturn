"""asyncpg connection pool."""
import os
import ssl
from urllib.parse import parse_qs, urlparse

import asyncpg

_pool: asyncpg.Pool | None = None


def _parse_dsn():
    """asyncpg doesn't understand ?ssl=true. Convert to ssl=True."""
    dsn = os.environ["DATABASE_URL"]
    u = urlparse(dsn)
    qs = parse_qs(u.query)
    ssl_required = qs.get("ssl", ["false"])[0].lower() in ("true", "1", "require")
    # strip ssl= from query
    new_query = "&".join(
        f"{k}={v[0]}" for k, v in qs.items() if k.lower() != "ssl"
    )
    new_dsn = f"{u.scheme}://{u.netloc}{u.path}"
    if new_query:
        new_dsn += "?" + new_query
    return new_dsn, ssl_required


async def init_pool() -> asyncpg.Pool:
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