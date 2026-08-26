from pathlib import Path


INDEX_HTML = Path(__file__).parents[1] / "app" / "templates" / "index.html"


def test_inventory_lookup_is_batch_and_hides_quantity_and_part_name():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "批量查询配件库存" in html
    assert 'split(/[\\s,，;；]+/)' in html
    assert "`${code}｜无库存`" in html
    assert "locations.join('、')" in html
    assert "locations.join('、') || '—'" in html
    assert ':src="result.imageUrl"' in html
    assert "`/api/inventory/image/${encodeURIComponent(r.part_code)}`" in html
    assert '@click="openImagePreview(result)"' in html
    assert '@keydown.escape.window="closeImagePreview()"' in html
    assert "max-height:88vh" in html
    assert 'draggable="false"' in html
    assert "user-select:none" in html
    assert 'loading="eager"' in html
    assert 'loading="lazy"' not in html
    assert "`${r.part_code} · 有 ${r.on_hand_qty}" not in html
    assert "${r.part_name || ''}" not in html
