"""Publish-time validation for document HTML.

Runs in the API on both create (version 1) and publish (new
version), so it gates every route in — MCP tools and CLI alike.
Copilot instructions alone cannot be enforced: an agent can
ignore them, and a user can publish via the CLI. This validator
is the deterministic gate, and it rejects with specific codes
so Copilot (or the author) can fix the exact problem and
republish. It never echoes matched content back in an error:
a violation message must never itself leak a secret.

What it rejects (400, html_validation_failed):
  secret_detected      a private key, bearer token, JWT, or a
                       secret assignment (api_key, client_secret,
                       access_token, refresh_token, private_key)
  token_url_in_html    a token URL or tokenUrl identifier baked
                       into the document; token URLs live in the
                       registry, never in HTML
  direct_fetch         a direct fetch()/XMLHttpRequest/axios/
                       WebSocket call; live data goes only via
                       window.agentdocs.apiFetch
  external_resource    an external script, stylesheet, image,
                       iframe, media, form action, CSS import,
                       or JS module import; documents are
                       self-contained (base64 assets inline)
  react_import         a React import / createElement in source;
                       documents are plain HTML, never React/JSX

What it deliberately does NOT reject:
  apiFetch to an unregistered app. Pages are allowed to fall
  back to sample data when an app is unregistered or a section
  returns 403, so rejecting that would break graceful-
  degradation pages. Registry membership is a Copilot-
  instructions workflow rule (list/register/sync first), not
  a publish gate. Plain <a href> links to other sites are
  also allowed; only resource loads that would fetch code,
  data, or tracking pixels are blocked.

Bundle-aware mode (Canvas React bundles, 2026-11-11):
  A bundled React page is still just one HTML file, so it goes
  through this same gate — but the naive version ran every regex
  over the whole file, and a minified bundle trips four false
  positives that are not violations at all:

  * ``fetch(`` inside Vite's modulepreload helper — dead code
    when the document has no <link rel="modulepreload">, so it
    can never fire. Any other ``fetch(`` is still rejected.
  * ``new URL("http://localhost")`` inside react-router — the
    tail matches the CSS ``url(http...)`` rule, but it is JS,
    not CSS, and nothing is fetched from it.
  * a ``<Link to=`` string inside bundled JS trips the
    ``<link href=`` tag check; a ``<script>`` string inside
    bundled JS trips the script-tag check.

  So the document is split first — markup (script/style
  bodies removed), <style> bodies, <script> bodies — and each
  check runs only in the context where it means something:
  tag checks on markup, CSS checks on styles (plus inline
  style attributes, which live in markup), JS checks on
  scripts. Secrets and token URLs still run over the whole
  file: a secret is a secret wherever it sits. The
  ``react_import`` rule runs on script bodies only: the
  minified bundle contains neither ``from 'react'`` nor
  ``React.createElement`` (JSX compiles to the jsx/jsxs
  runtime), so a real React import in source still fails
  while a bundle passes. This validator sees only the HTML,
  not the source, so it cannot tell whether a ``fetch(`` in
  the bundle came from app code or the builder — the helper
  allowlist is the narrow exception, and it only applies
  when the helper pattern matches exactly and no
  modulepreload link exists to make it live.
"""

from __future__ import annotations

import re

_SCRIPT_RE = re.compile(
    r"(<script[^>]*>)[\s\S]*?(</script>)", re.IGNORECASE
)
_STYLE_RE = re.compile(r"(<style[^>]*>)[\s\S]*?(</style>)", re.IGNORECASE)
_SCRIPT_BODY_RE = re.compile(r"<script[^>]*>([\s\S]*?)</script>", re.IGNORECASE)
_STYLE_BODY_RE = re.compile(r"<style[^>]*>([\s\S]*?)</style>", re.IGNORECASE)
_MODULEPRELOAD_RE = re.compile(
    r"<link\b[^>]*\brel\s*=\s*['\"]modulepreload['\"]", re.IGNORECASE
)
_FETCH_RE = re.compile(r"\bfetch\s*\(")
# Bridge calls are not direct fetches: the page holds no token and the
# outer shell performs the call. The v2 name is window.canvas.api.fetch;
# the old name window.agentdocs.apiFetch never matched _FETCH_RE (capital
# F), so only the v2 name needs carving out — agentdocs.api.fetch is
# allowed here too, in case the old name is ever written that way.
_BRIDGE_FETCH_RE = re.compile(
    r"\b(?:canvas|agentdocs)\s*\.\s*api\s*\.\s*fetch\s*\("
)
# Vite's modulepreload helper, exactly as emitted: fetch(d.href, m).
# Allow optional whitespace so a reformatted bundle still matches;
# anything else shaped like fetch(<expr>) is not the helper.
_HELPER_FETCH_RE = re.compile(
    r"fetch\(\s*[A-Za-z_$][\w$]*\.href\s*,\s*[A-Za-z_$][\w$]*\s*\)"
)

# Each entry: (code, message, scope, [patterns]). Scope names which
# part of the split document the patterns run on:
#   "whole"          the unsplit file (secrets, token URLs)
#   "markup"         tags and attributes, script/style bodies removed
#   "styles"         <style> bodies only
#   "markup+styles" CSS checks — styles plus inline style attributes
#   "scripts"        <script> bodies only
#   "markup+scripts" JS checks that could also sit in an attribute
# Patterns are matched case-insensitively except where noted;
# matched text is never returned, only the code.
_CHECKS: list[tuple[str, str, str, list[re.Pattern]]] = [
    (
        "secret_detected",
        "HTML contains a secret or token value; remove it and fetch data at view time via apiFetch instead",
        "whole",
        [
            re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
            re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]{20,}"),
            re.compile(r"eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"),
            re.compile(
                r"(?:api_key|apikey|client_secret|access_token|refresh_token|private_key)"
                r"\s*[:=]\s*['\"][^'\"\s]{4,}['\"]",
                re.IGNORECASE,
            ),
            re.compile(r"Authorization\s*['\"]?\s*[:=]", re.IGNORECASE),
        ],
    ),
    (
        "token_url_in_html",
        "HTML contains a token URL; token URLs live in the API registry, never in a document",
        "whole",
        [
            re.compile(r"\btokenUrl\b"),
            re.compile(r"\btoken_url\b"),
            re.compile(r"https?://[^\s'\"<>]*?/token(?:[/?#'\"<>]|$)", re.IGNORECASE),
        ],
    ),
    (
        "direct_fetch",
        "Live data must go via window.agentdocs.apiFetch, never a direct fetch, XMLHttpRequest, axios, or WebSocket call",
        "markup+scripts",
        [
            re.compile(r"\bXMLHttpRequest\b"),
            re.compile(r"\baxios\s*\.\s*(?:get|post|put|delete|request)\s*\("),
            re.compile(r"\bWebSocket\s*\("),
        ],
    ),
    (
        "external_resource",
        "Documents must be self-contained; no external scripts, stylesheets, images, iframes, media, form actions, CSS imports, or JS module imports",
        "markup",
        [
            re.compile(
                r"<(?:script|link|img|image|iframe|source|video|audio|embed)\b"
                r"[^>]*\b(?:src|href)\s*=\s*['\"](?:https?:)?//",
                re.IGNORECASE,
            ),
            re.compile(
                r"<form\b[^>]*\baction\s*=\s*['\"](?:https?:)?//",
                re.IGNORECASE,
            ),
        ],
    ),
    (
        "external_resource",
        "Documents must be self-contained; no external scripts, stylesheets, images, iframes, media, form actions, CSS imports, or JS module imports",
        "markup+styles",
        [
            re.compile(r"@import\s+['\"]?https?:", re.IGNORECASE),
            re.compile(r"url\(\s*['\"]?https?:", re.IGNORECASE),
        ],
    ),
    (
        "external_resource",
        "Documents must be self-contained; no external scripts, stylesheets, images, iframes, media, form actions, CSS imports, or JS module imports",
        "scripts",
        [
            re.compile(r"\bimport\s+(?:[^'\"]*?\sfrom\s+)?['\"]https?:", re.IGNORECASE),
        ],
    ),
    (
        "react_import",
        "Documents are plain HTML, never React/JSX",
        "scripts",
        [
            re.compile(r"\bfrom\s+['\"]react(?:-dom)?['\"]"),
            re.compile(r"\bReact\s*\.\s*createElement\b"),
        ],
    ),
]


def _split(html: str) -> tuple[str, str, str]:
    """Split a document into (markup, styles, scripts).

    Markup is the document with every <script> and <style> body
    removed — the opening tags stay, so an external ``src`` on a
    <script> tag is still caught — so tag checks see real tags
    only, not a ``<link>`` or ``<script>`` string inside bundled
    JS. Styles and scripts are the concatenated bodies of their
    respective elements.
    """
    scripts = "\n".join(_SCRIPT_BODY_RE.findall(html))
    styles = "\n".join(_STYLE_BODY_RE.findall(html))
    markup = _SCRIPT_RE.sub(r"\1\2", html)
    markup = _STYLE_RE.sub(r"\1\2", markup)
    return markup, styles, scripts


def _has_real_fetch(scripts: str, markup: str) -> bool:
    """True if the document makes a direct fetch() that is not
    Vite's dead modulepreload helper or a bridge call.

    A fetch( in markup (e.g. an event-handler attribute) is
    always real, unless it is a bridge call. In scripts, only the helper
    ``fetch(<ident>.href, <ident>)`` and bridge calls
    ``window.canvas.api.fetch(...)`` are allowed — and the helper only
    when the document has no <link rel="modulepreload"> that could
    make the helper fire. One helper call in a document with a
    modulepreload link is a live fetch, not dead code.
    """
    if len(_FETCH_RE.findall(markup)) > len(_BRIDGE_FETCH_RE.findall(markup)):
        return True
    total = len(_FETCH_RE.findall(scripts))
    if total == 0:
        return False
    if _MODULEPRELOAD_RE.search(markup):
        return True
    helper = len(_HELPER_FETCH_RE.findall(scripts))
    bridge = len(_BRIDGE_FETCH_RE.findall(scripts))
    return total > helper + bridge


def validate_document_html(html: str) -> list[dict]:
    """Return a list of violations (empty = publishable).

    One entry per violated rule, never per match, and no matched
    text is included — a secret must not come back in the error.
    """
    markup, styles, scripts = _split(html)
    parts = {
        "whole": html,
        "markup": markup,
        "styles": styles,
        "markup+styles": markup + "\n" + styles,
        "scripts": scripts,
        "markup+scripts": markup + "\n" + scripts,
    }
    violations: list[dict] = []
    seen: set[str] = set()
    for code, message, scope, patterns in _CHECKS:
        text = parts[scope]
        if any(p.search(text) for p in patterns):
            if code not in seen:
                seen.add(code)
                violations.append({"code": code, "message": message})
    if _has_real_fetch(scripts, markup):
        if "direct_fetch" not in seen:
            violations.append(
                {
                    "code": "direct_fetch",
                    "message": "Live data must go via window.agentdocs.apiFetch, "
                    "never a direct fetch, XMLHttpRequest, axios, or WebSocket call",
                }
            )
    return violations
