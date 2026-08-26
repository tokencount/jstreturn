from pathlib import Path


INDEX_HTML = Path(__file__).parents[1] / "app" / "templates" / "index.html"


def test_inventory_lookup_is_batch_and_hides_quantity_and_part_name():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "批量查询配件库存" in html
    assert 'split(/[\\s,，;；]+/)' in html
    assert "`${code}｜无库存`" in html
    assert "locations.join('、')" in html
    assert "`${r.part_code} · 有 ${r.on_hand_qty}" not in html
    assert "${r.part_name || ''}" not in html
