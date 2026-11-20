"""Every page is dark, always: no light theme, no theme switch, nothing that follows the system setting."""
import re

LIGHT = re.compile(r'prefers-color-scheme|data-theme|color-scheme:\s*light|light dark|mb-theme', re.I)


def test_signed_in_pages_are_dark_only(browser):
    for path in ("/bets/", "/bets/settings", "/bets/help"):
        html = browser.get(path).text
        assert '<meta name="color-scheme" content="dark">' in html
        assert '<meta name="theme-color" content="#0f1012">' in html
        assert not LIGHT.search(html), path


def test_login_page_and_assets_are_dark_only(anon):
    html = anon.get("/bets/login").text
    assert '<meta name="color-scheme" content="dark">' in html and not LIGHT.search(html)
    css = anon.get("/bets/static/design.css").text
    assert "color-scheme:dark" in css and not LIGHT.search(css)
    assert "#FFFFFF" not in css.upper()
    manifest = anon.get("/bets/manifest.webmanifest").json()
    assert manifest["background_color"] == manifest["theme_color"] == "#0f1012"


def test_app_js_has_no_theme_switch(browser):
    js = browser.get("/bets/static/app.js").text
    assert not LIGHT.search(js) and "data-theme-toggle" not in js
