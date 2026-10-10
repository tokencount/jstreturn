from pathlib import Path


HTML = (Path(__file__).parents[1] / "app" / "templates" / "index.html").read_text()


def test_pending_has_explicit_replacement_sku_entry():
    assert HTML.count("更换配件 SKU") >= 2
    assert "配件 SKU (Part Code)" in HTML


def test_returns_and_admin_can_see_pending_complete_action_desktop_and_mobile():
    assert "row.it.status==='PENDING' && ['returns','admin'].includes(user.role)" in HTML
    assert "it.status === 'PENDING' && ['returns','admin'].includes(user.role)" in HTML


def test_pending_complete_confirmation_warns_about_override():
    assert "此单仍是 PENDING，将手动转为 COMPLETED" in HTML
