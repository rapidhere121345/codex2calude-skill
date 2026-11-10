"""Static-source regression: every authorized principal is selectable and labelled."""
from pathlib import Path

STATIC = Path(__file__).parents[1] / "src/mcp_audit_dashboard/static"
PRINCIPALS = ("owner", "partner", "partner2", "partner3", "partner4")


def test_principal_labels_cover_all_five_roles():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for principal in PRINCIPALS:
        assert f'["{principal}",' in app
    assert '["total",' in app


def test_provider_card_loop_covers_all_five_roles_before_total():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    loop = '["owner", "partner", "partner2", "partner3", "partner4", "total"]'
    assert loop in app


def test_principal_select_offers_all_five_roles_after_partner2():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for principal in PRINCIPALS:
        assert f'<option value="{principal}">' in html
    assert html.index('value="partner2"') < html.index('value="partner3"')
    assert html.index('value="partner3"') < html.index('value="partner4"')
