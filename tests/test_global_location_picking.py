from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from app.picking import compute_location_pick_plan


def item(i, pallet='A', qty=1, status='READY', parts=None):
    return dict(id=i, pallet_no=pallet, status=status,
                created_at=datetime(2026, 1, i, tzinfo=timezone.utc),
                parts=parts if parts is not None else [dict(part_code='P', qty=qty)])


def run(items, locs, total=99, fallback='LEGACY'):
    return compute_location_pick_plan(items, {'P': {'qty': total, 'location': fallback}},
                                      [dict(part_code='P', location=l, qty=q) for l,q in locs])


def counts(plan):
    out=Counter()
    for cs in plan['by_item'].values():
        for c,ls in cs.items():
            for l in ls:out[c,l['location']]+=l['qty']
    return out


def test_non_e_only_mixed_competition_no_reuse_and_no_double_snapshot():
    rows=[item(1,qty=2),item(2,'B',qty=2)]
    before=deepcopy(rows)
    p=run(rows,[('E1',1),('B1',3)], total=400)
    assert not p['shortages']
    assert counts(p)=={('P','E1'):1,('P','B1'):3}
    assert rows==before
    assert run(list(reversed(rows)),[('B1',3),('E1',1)],400)==p
    assert counts(run([item(1)], [('NON-E',4)]))=={('P','NON-E'):1}


def test_duplicates_sum_once_and_zero_negative_no_legacy_topup():
    p=run([item(1,qty=3),item(2,'B')],[('B1',1),('B1',2),('E1',0),('E2',-9)],999)
    assert counts(p)=={('P','B1'):3}
    assert len(p['shortages'])==1
    assert run([item(1)], [('E',0)],999)['shortages']=={1:{'P':1}}


def test_legacy_positive_location_only_and_blank_fail_closed():
    assert counts(run([item(1)],[],2,'BIN-LEGACY'))=={('P','BIN-LEGACY'):1}
    assert run([item(1)],[],2,'')['shortages']=={1:{'P':1}}
    assert run([item(1)],[('',2)],2,'GOOD')['shortages']=={1:{'P':1}}
    assert run([item(1)],[],-1,'GOOD')['shortages']=={1:{'P':1}}


def test_duplicate_part_demands_and_atomic_no_partial():
    p=run([item(1,parts=[dict(part_code='P',qty=1),dict(part_code='P',qty=2)])],[('B',3)])
    assert counts(p)=={('P','B'):3}
    p=run([item(1,parts=[dict(part_code='P',qty=1),dict(part_code='MISSING',qty=1)])],[('B',3)])
    assert p['by_item'][1]=={} and p['shortages']=={1:{'MISSING':1}}


def test_pending_completed_excluded_and_pallet_priority_preserved():
    rows=[item(1,'LOW',qty=1),item(2,'LOW',status='PENDING',qty=50),item(3,'HIGH'),item(4,'DONE',status='COMPLETED')]
    p=run(rows,[('B',1)])
    assert p['by_item'][3]['P']==[dict(location='B',qty=1)]
    assert p['shortages']=={1:{'P':1}}
    assert 2 not in p['by_item'] and 4 not in p['by_item']
