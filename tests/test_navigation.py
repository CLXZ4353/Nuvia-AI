from src.navigation import render_sidebar, render_sidebar_css


def test_sidebar_keeps_requested_icon_order_and_marks_active_section():
    page = render_sidebar("saved")

    assert page.index("Notizie Salvate") < page.index("Impostazioni") < page.index("Cronologia")
    assert 'href="/?sidebar=saved"' in page
    assert 'data-sidebar-target="/?sidebar=saved"' in page
    assert 'href="/settings"' in page
    assert 'href="/?sidebar=open"' in page
    assert 'data-sidebar-target="/?sidebar=open"' in page
    assert 'href="/history"' not in page
    assert page.count('aria-current="page"') == 1
    assert 'aria-label="Apri notizie salvate"' in page
    assert 'aria-label="Apri cronologia"' in page


def test_sidebar_has_open_transition_and_closed_icon_navigation_behavior():
    css = render_sidebar_css()
    page = render_sidebar()

    assert "body.sidebar-open .app-sidebar" in css
    assert "transition: width 180ms ease" in css
    assert "body.classList.add(\"sidebar-open\")" in page
    assert "window.location.assign(target)" in page
    assert "event.key === \"Escape\"" in page
    assert ".history-panel" in css
    assert "body.sidebar-open .history-panel" in css
    assert ".history-list" in css
