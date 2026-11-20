"""Execute production inventory SQL, not a mocked stock aggregation."""
from datetime import datetime, timezone
import duckdb
import pytest
from app import matcher
from app.routers.exports import compute_purchase_rows

def inventory(snapshot, locations):
    db = duckdb.connect(':memory:')
    db.execute('CREATE TABLE inventory_snapshot(part_code TEXT, location TEXT, on_hand_qty INT)')
    db.execute('CREATE TABLE inventory_locations(part_code TEXT, location TEXT, qty INT)')
    if snapshot:
        db.executemany('INSERT INTO inventory_snapshot VALUES (?, ?, ?)', snapshot)
    if locations:
        db.executemany('INSERT INTO inventory_locations VALUES (?, ?, ?)', locations)
    result = dict(db.execute(matcher.ELIGIBLE_INVENTORY_SQL).fetchall())
    db.close()
    return result

def ticket(i, qty=1):
    return {'id':i,'pallet_no':str(i),'status':'PENDING',
            'created_at':datetime(2026,11,20,tzinfo=timezone.utc),
            'parts':[{'part_code':'HS-P','qty':qty}]}

@pytest.mark.parametrize('locations,total', [
    ([('HS-P','A1',3)],3),
    ([('HS-P','E1',2),('HS-P','A1',3)],5),
    ([('HS-P','A1',2),('HS-P','A1',3),('HS-P','B1',1)],6),
    ([('HS-P','A1',0),('HS-P','E1',-3)],0),
    ([('HS-P','A1',4),('HS-P','B1',-2)],4),
])
def test_all_locations_aggregate_once_without_snapshot_double_count(locations,total):
    stock = inventory([('HS-P','E1',999)],locations)
    assert stock == {'HS-P':total}
    plan = matcher.compute_reservation_plan([ticket(1,3)],stock)
    assert (1 in plan['ready_ids']) == (total >= 3)
    assert compute_purchase_rows([ticket(1,3)],stock)['by_code'] == ({} if total >= 3 else {'HS-P':3-total})

@pytest.mark.parametrize('location,qty,total',[('A1',5,5),('E1',4,4),('',3,3),('A1',-2,0)])
def test_snapshot_only_fallback_is_location_independent(location,qty,total):
    assert inventory([('HS-P',location,qty)],[]) == {'HS-P':total}

def test_competing_tickets_and_purchase_shortage_agree():
    stock = inventory([('HS-P','E1',100)],[('HS-P','A1',2),('HS-P','E1',1)])
    items = [ticket(1,2),ticket(2,2),ticket(3,2)]
    plan = matcher.compute_reservation_plan(items,stock)
    assert len(plan['ready_ids']) == 1
    assert plan['reserved_by_part'] == {'HS-P':2}
    assert plan['available_by_part'] == {'HS-P':1}
    # Preserve existing per-ticket shortage semantics (not a new purchase policy).
    assert compute_purchase_rows(items,stock)['by_code'] == {'HS-P':2}
    assert sum(plan['reserved_by_part'].values()) <= stock['HS-P']

def test_export_uses_identical_inventory_query():
    from app.routers import exports
    assert exports.ELIGIBLE_INVENTORY_SQL == matcher.ELIGIBLE_INVENTORY_SQL
