"""Bulk import of defective items from CSV.

CSV / Excel format — one row per part, pallet_no repeats within a group:

    pallet_no, product_name?, sku, qty, part_code, part_name?, part_qty

Rules:
- Rows with the same pallet_no form ONE defective_item.
- Empty/whitespace pallet_no row is skipped.
- The header row (first row) is auto-detected by checking for known column
  names; if detected, the rest of the file is treated as data. If no header
  is detected, the file is treated as data with NO header (the first row's
  values are interpreted as data).
- Names are flexible: 中文 编码/配件编码/SKU/配件名称/... or English.

Returns a summary with per-ticket successes / failures.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.auth import require_role
from app.db import pool
from app.matcher import evaluate_status

router = APIRouter(prefix="/api/imports", tags=["imports"])


# ---------- helpers ----------
HEADER_TOKENS = (
    "pallet", "sku", "part_code", "编码", "配件编码",
    "qty", "数量", "qty", "product_name", "产品名称",
)

def _split_csv(text: str):
    """Yield rows as lists of stripped cells, skipping pure-empty rows."""
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        cells = [(c or "").strip() for c in row]
        if any(cells):
            yield cells


def _looks_header(row) -> bool:
    """Header row = the cells contain known column names."""
    joined = " ".join(row).lower()
    return any(tok in joined for tok in HEADER_TOKENS)


def _find_col(fieldnames, *aliases):
    for a in aliases:
        a_norm = a.lower()
        if a_norm in fieldnames:
            return a_norm
    return None


def _group_tickets(rows):
    """Group rows by pallet_no. Returns dict: pallet_no -> list of rows."""
    groups = defaultdict(list)
    for r in rows:
        groups[r.get("pallet_no") or ""].append(r)
    return groups


# ---------- main endpoint ----------
@router.post("/defectives")
async def upload_defectives(
    file: UploadFile = File(...),
    user: dict = Depends(require_role("returns", "admin")),
):
    if not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(400, "must be .csv or .txt")

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")

    rows = list(_split_csv(text))
    if not rows:
        raise HTTPException(400, "empty file")

    has_header = _looks_header(rows[0])
    data_rows = rows[1:] if has_header else rows

    if not data_rows:
        raise HTTPException(400, "no data rows")

    # Build per-column inference.
    # Approach: if no header, assume canonical order:
    #   [pallet_no, product_name, sku, qty, part_code, part_name, part_qty]
    if has_header:
        # normalize headers
        headers = [c.lower().strip() for c in rows[0]]
        n = max(len(headers), 8)
        # pad
        if len(headers) < n:
            headers = headers + [""] * (n - len(headers))

        def col(*aliases):
            return _find_col(headers, *aliases)

        # Cc's spreadsheet columns (uppercased keys handled below).
        #   DATE        | PALLET | 次品仓位 | 商品名称 | SKU | part_code | part_quantity
        # Header detection uses lowercased headers, so we accept both cases.
        pallet_idx = col("pallet_no", "pallet", "pltn", "pltn0", "退件号", "板号", "pallet no.", "pallet no") or 1
        prod_idx = col("product_name", "product", "name", "产品名称", "商品名称", "产品") or 3
        sku_idx = col("sku", "model", "型号") or 4
        # part_quantity might be labelled differently
        part_qty_idx = col("part_qty", "qty_part", "part_quantity", "配件数量", "quantity", "qty") or 7
        qty_idx = col("qty", "quantity", "数量", "qty_main")  # ticket qty (default to 1)
        # qty default to 1 when no header detected; if no header col, leave None.
        if qty_idx is None:
            qty_idx = part_qty_idx
        location_idx = col("location", "次品仓位", "仓位", "warehouse", "loc", "位置")
        part_idx = col("part_code", "part", "编码", "配件编码", "jst_code", "part code")
        part_name_idx = col("part_name", "配件名称", "name_part")
    else:
        # Canonical positional order
        pallet_idx, prod_idx, sku_idx, qty_idx = 1, 3, 4, 6
        part_idx, part_name_idx, part_qty_idx = 6, 5, 7
        location_idx = 2

    # Convert each data row to a structured dict.
    parsed = []
    parse_failures = []
    for line_no, row in enumerate(data_rows, start=2 if has_header else 1):
        def at(i):
            return row[i].strip() if i < len(row) else ""
        pallet = at(pallet_idx)
        if not pallet:
            continue
        part_code = at(part_idx)
        if not part_code:
            parse_failures.append({"line": line_no, "reason": "missing part_code", "pallet": pallet})
            continue
        try:
            part_qty = int(float(at(part_qty_idx) or 0))
        except ValueError:
            part_qty = 0
        if part_qty <= 0:
            parse_failures.append({"line": line_no, "reason": "bad part_qty", "pallet": pallet})
            continue
        try:
            qty = int(float(at(qty_idx) or 1))
        except ValueError:
            qty = 1
        parsed.append({
            "pallet_no": pallet,
            "product_name": at(prod_idx) or None,
            "sku": at(sku_idx),
            "qty": qty if qty > 0 else 1,
            "part_code": part_code,
            "part_name": at(part_name_idx) or None,
            "part_qty": part_qty,
            "location": at(location_idx) if location_idx is not None else None,
            "_line": line_no,
        })

    if not parsed:
        raise HTTPException(400, f"no usable rows parsed. failures: {parse_failures[:5]}")

    # Group by pallet_no, validate each has at least one valid row.
    groups = _group_tickets(parsed)
    tickets = {}
    for pallet, items in groups.items():
        sku = items[0]["sku"]
        product_name = items[0]["product_name"]
        qty = items[0]["qty"]
        location = items[0]["location"]
        parts = [{
            "part_code": it["part_code"],
            "part_name": it["part_name"],
            "qty": it["part_qty"],
        } for it in items]
        tickets[pallet] = {
            "sku": sku,
            "product_name": product_name,
            "qty": qty,
            "location": location,
            "parts": parts,
        }

    # Now INSERT per ticket, transaction-safe per ticket.
    successes = []
    failures = []

    async with pool().acquire() as conn:
        for pallet, t in tickets.items():
            if not t["sku"]:
                failures.append({"pallet": pallet, "error": "missing sku"})
                continue
            try:
                async with conn.transaction():
                    di_id = await conn.fetchval(
                        """
                        INSERT INTO defective_items (pallet_no, product_name, sku, qty, location, created_by)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING id
                        """,
                        pallet, t["product_name"], t["sku"], t["qty"], t["location"], user["id"],
                    )
                    for p in t["parts"]:
                        await conn.execute(
                            """
                            INSERT INTO defective_parts (defective_id, part_code, part_name, qty)
                            VALUES ($1, $2, $3, $4)
                            """,
                            di_id, p["part_code"], p["part_name"], p["qty"],
                        )
                    await conn.execute(
                        """
                        INSERT INTO audit_log (user_id, action, entity_type, entity_id, details)
                        VALUES ($1, 'import', 'defective_item', $2, $3::jsonb)
                        """,
                        user["id"], di_id,
                        json.dumps({"source": "csv", "parts": len(t["parts"])}),
                    )
                # Re-evaluate status outside the transaction (separate asyncpg acquisition).
                status = await evaluate_status(di_id)
                successes.append({
                    "id": di_id,
                    "pallet": pallet,
                    "sku": t["sku"],
                    "status": status,
                    "parts": len(t["parts"]),
                })
            except Exception as e:
                failures.append({"pallet": pallet, "error": str(e) or "failed"})

    return {
        "summary": {
            "submitted_tickets": len(tickets),
            "submitted_rows": len(parsed),
            "succeeded": len(successes),
            "failed": len(failures),
            "parse_failures_count": len(parse_failures),
            "header_detected": has_header,
        },
        "successes": successes,
        "failures": failures,
        "parse_failures_sample": parse_failures[:10],
    }


@router.get("/template")
async def template_csv(user: dict = Depends(require_role("returns", "admin"))):
    """Return a CSV template the user can download.

    Column header styles supported:
      - DATE/PALLET/次品仓位/商品名称/SKU/part_code/part_quantity (Cc's spreadsheet layout)
      - pallet_no/product_name/sku/qty/part_code/part_name/part_qty (canonical English)
      - 退件号/产品名称/型号/编码/配件名称/配件数量 (Chinese aliases)
    """
    csv_text = (
        "DATE,PALLET,次品仓位,商品名称,SKU,part_code,part_quantity\n"
        "13/6/2026,PLT-001,H5-66-4,钓鱼伞,SKU-3301,P-CODE-1,1\n"
        "13/6/2026,PLT-001,H5-66-4,钓鱼伞,SKU-3301,P-CODE-2,1\n"
        "13/6/2026,PLT-002,H5-67-3,碳钢蛋卷桌,SKU-3302,P-CODE-3,2\n"
    )
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="defectives_template.csv"'},
    )
