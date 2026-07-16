"""Cron-triggered endpoints. Auth: X-Cron-Secret header."""
from __future__ import annotations

import hmac
import os
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException

from app.db import pool
from app.matcher import list_with_parts, reevaluate_all_pending_ready, summary_counts
from app.telegram import send_to_admins

router = APIRouter(prefix="/cron", tags=["cron"])


def check_cron_secret(x_cron_secret: str = Header(...)):
    expected = os.environ.get("CRON_SECRET", "")
    if not expected or not hmac.compare_digest(x_cron_secret, expected):
        raise HTTPException(401, "bad cron secret")


@router.post("/daily-summary", dependencies=[Depends(check_cron_secret)])
async def daily_summary():
    """09:00 and 18:00. Recompute all PENDING/READY statuses, send a summary."""
    await reevaluate_all_pending_ready()

    counts = await summary_counts()
    items = await list_with_parts(limit=200)

    pending_short: list[dict] = []
    ready: list[str] = []
    for it in items:
        if it["status"] == "PENDING":
            for p in it.get("parts", []):
                if p.get("short", 0) > 0:
                    pending_short.append({
                        "pallet": it["pallet_no"],
                        "sku": it["sku"],
                        "part_code": p["part_code"],
                        "need": p["need"],
                        "have": p["have"],
                        "short": p["short"],
                    })
        elif it["status"] == "READY":
            ready.append(f"  #{it['id']} {it['pallet_no']} / {it['sku']} ×{it['qty']}")

    today = date.today().isoformat()
    lines = [f"📦 <b>jstreturn 日报 — {today}</b>", ""]
    lines.append(f"🟢 READY: <b>{counts['READY']}</b>")
    lines.append(f"🔴 PENDING: <b>{counts['PENDING']}</b>")
    lines.append(f"✅ COMPLETED (历史): <b>{counts['COMPLETED']}</b>")
    lines.append("")

    if ready:
        lines.append("<b>可组装：</b>")
        lines.extend(ready[:20])
        if len(ready) > 20:
            lines.append(f"  …还有 {len(ready) - 20} 个")
        lines.append("")

    if pending_short:
        lines.append("<b>缺料 TOP:</b>")
        pending_short.sort(key=lambda x: -x["short"])
        for s in pending_short[:15]:
            lines.append(
                f"  {s['pallet']} {s['part_code']} "
                f"需{s['need']} / 有{s['have']} / 缺<b>{s['short']}</b>"
            )
    else:
        lines.append("✅ 没有缺料")

    text = "\n".join(lines)
    sent = await send_to_admins(text)
    return {"counts": counts, "telegram_sent": sent, "text_len": len(text)}


@router.post("/pull-inventory", dependencies=[Depends(check_cron_secret)])
async def pull_inventory():
    """Placeholder. Real JST fetcher is pending Cc's instructions."""
    return {
        "ok": False,
        "reason": "JST fetcher not implemented yet (waiting for Cc's instructions)",
    }