"""Read-only, global per-READY-item location reservations for pallet sheets.

SKU reservation priority is unchanged. Physical locations are shared globally,
not re-used independently for every pallet. This is a projection, not a stock
movement or a persistent reservation ledger.
"""
from collections import defaultdict
from datetime import datetime, timezone
from fractions import Fraction
from app.matcher import _allocate_within_pallet, _demands


def compute_location_pick_plan(items, snapshots, locations):
    by_code = defaultdict(list)
    for row in locations:
        by_code[row['part_code']].append(row)
    capacities = {}
    stock = {}
    for code, snapshot in snapshots.items():
        child = by_code.get(code)
        if child is not None:
            merged = defaultdict(int)
            for row in child:
                if int(row['qty']) > 0:
                    merged[str(row['location'] or '').strip()] += int(row['qty'])
            capacities[code] = dict(merged)
            stock[code] = sum(merged.values())
        else:
            qty = max(int(snapshot.get('qty') or 0), 0)
            location = str(snapshot.get('location') or '').strip()
            capacities[code] = {location: qty} if qty else {}
            stock[code] = qty

    # Same pallet projection/ranking as the matcher; no READY/status mutation.
    grouped = defaultdict(list)
    for raw in items:
        if raw['status'] not in ('READY', 'PENDING'):
            continue
        grouped[str(raw.get('pallet_no') or '')].append({
            'id': int(raw['id']), 'status': raw['status'],
            'created_at': raw.get('created_at') or datetime.max.replace(tzinfo=timezone.utc),
            'demands': _demands(raw.get('parts') or []),
        })
    ranked = []
    for pallet, rows in grouped.items():
        projected = _allocate_within_pallet(rows, stock)
        ranked.append((pallet, rows, Fraction(len(projected), len(rows)), len(projected),
                       min(r['created_at'] for r in rows)))
    ranked.sort(key=lambda r: (-r[2], -r[3], r[4], r[0]))
    remaining = {c: dict(ls) for c, ls in capacities.items()}
    assigned, shortages = {}, {}
    for _, rows, _, _, _ in ranked:
        # Only persisted READY items can appear on a pick sheet. Order within
        # their pallet is stable, with the matcher's scarcity tie-break.
        waiting = [r for r in rows if r['status'] == 'READY']
        while waiting:
            totals = {c: sum(q for l, q in ls.items() if l) for c, ls in remaining.items()}
            def cost(r):
                return (sum(Fraction(q, max(totals.get(c, 0), 1)) for c, q in r['demands'].items()),
                        sum(r['demands'].values()), r['created_at'], r['id'])
            feasible = [r for r in waiting if r['demands'] and all(totals.get(c, 0) >= q for c, q in r['demands'].items())]
            chosen = min(feasible or waiting, key=cost)
            rid = chosen['id']
            shortage = {c: max(q - totals.get(c, 0), 0) for c, q in chosen['demands'].items()
                        if q > totals.get(c, 0)}
            if not chosen['demands']:
                shortage = {'': 1}
            if shortage:
                shortages[rid] = shortage
                assigned[rid] = {}
            else:
                assigned[rid] = {}
                for c, qty in chosen['demands'].items():
                    picks = []
                    # All imported Main Bin+Pack positive locations, not E-only.
                    for location, available in sorted(remaining.get(c, {}).items(), key=lambda x: (-x[1], x[0])):
                        if not location or available <= 0:
                            continue
                        take = min(qty, available)
                        if take:
                            picks.append({'location': location, 'qty': take})
                            remaining[c][location] -= take
                            qty -= take
                        if not qty:
                            break
                    assigned[rid][c] = picks
            waiting.remove(chosen)
    return {'by_item': assigned, 'shortages': shortages}
