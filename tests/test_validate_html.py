"""Unit tests for the bundle-aware publish gate.

Covers step 2 of the Canvas React bundle guide
(goals/agent-docs-for-wellington/files/canvas-react-bundle-guide.md):
the prototype's dist/index.html must pass the API validator, while
a bundle with a real fetch() in app code must still fail. The
false-positive traps the split exists for are pinned here so a
future regex change cannot silently reintroduce them. Step 5 of the
same guide: a window.canvas.api.fetch bridge call must pass, while a
bridge call next to a real fetch() must still fail.
"""

from pathlib import Path

from app.validate_html import validate_document_html

BUNDLE = Path(__file__).parent.parent.parent / (
    "goals/agent-docs-for-wellington/files/canvas-react-bundle/dist/index.html"
)


def _codes(html: str) -> set[str]:
    return {v["code"] for v in validate_document_html(html)}


def test_react_bundle_passes():
    assert BUNDLE.exists(), "prototype bundle missing — rebuild it first"
    assert _codes(BUNDLE.read_text(encoding="utf-8")) == set()


def test_bundle_with_real_fetch_still_fails():
    html = BUNDLE.read_text(encoding="utf-8")
    bad = html.replace(
        "fetch(d.href,m)", "fetch(d.href,m);fetch('https://internal.example/api')"
    )
    assert bad != html
    assert "direct_fetch" in _codes(bad)


def test_bundle_helper_fetch_is_live_when_modulepreload_exists():
    html = BUNDLE.read_text(encoding="utf-8")
    live = html.replace(
        "</head>", '<link rel="modulepreload" href="/x.js"></head>'
    )
    assert "direct_fetch" in _codes(live)


def test_js_string_traps_do_not_fire():
    html = (
        "<html><body><script>"
        "const s='<Link to=\"/about\">x</Link>';"
        "const t='<script src=\"x\">';"
        'const u=new URL("http://localhost");'
        "</script></body></html>"
    )
    assert _codes(html) == set()


def test_plain_html_bad_cases_still_rejected():
    bad_cases = {
        "secret_detected": "<html><body><script>const api_key = \"abc12345\";</script></body></html>",
        "direct_fetch": "<html><body><script>fetch('https://internal.example/api');</script></body></html>",
        "external_resource": "<html><head><script src=\"https://cdn.example/lib.js\"></script></head><body>hi</body></html>",
        "token_url_in_html": "<html><body><script>const u = 'https://internal.example/oauth/token';</script></body></html>",
        "react_import": "<html><body><script type=\"module\">import React from 'react';</script></body></html>",
    }
    for code, html in bad_cases.items():
        assert code in _codes(html), code


def test_real_css_url_and_inline_style_still_rejected():
    assert "external_resource" in _codes(
        "<html><head><style>.a{background:url(http://example.com/x.png)}</style></head>"
        "<body>hi</body></html>"
    )
    assert "external_resource" in _codes(
        '<html><body><div style="background:url(https://x.example/a.png)">hi</div></body></html>'
    )


def test_bridge_fetch_is_allowed():
    # Step 5 of the Canvas React bundle guide: a page that calls the
    # bridge (v2 name window.canvas.api.fetch) must publish. The old
    # name window.agentdocs.apiFetch never matched the fetch rule.
    assert _codes(
        "<html><body><script>"
        "window.canvas.api.fetch('riskhub', '/v1/docs').then(r => {});"
        "</script></body></html>"
    ) == set()
    # A bridge call in an event-handler attribute is still a bridge call.
    assert _codes(
        '<html><body><button onclick="window.canvas.api.fetch(\'riskhub\', \'/v1/docs\')">x</button></body></html>'
    ) == set()


def test_bridge_fetch_plus_real_fetch_still_fails():
    assert "direct_fetch" in _codes(
        "<html><body><script>"
        "window.canvas.api.fetch('riskhub', '/v1/docs');"
        "fetch('https://internal.example/api');"
        "</script></body></html>"
    )
