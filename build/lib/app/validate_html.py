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
  react_import         a React import / createElement; documents
                       are plain HTML, never React/JSX

What it deliberately does NOT reject:
  apiFetch to an unregistered app. Pages are allowed to fall
  back to sample data when an app is unregistered or a section
  returns 403, so rejecting that would break graceful-
  degradation pages. Registry membership is a Copilot-
  instructions workflow rule (list/register/sync first), not
  a publish gate. Plain <a href> links to other sites are
  also allowed; only resource loads that would fetch code,
  data, or tracking pixels are blocked.
"""

from __future__ import annotations

import re

# Each entry: (code, message, [patterns]). Patterns are matched
# case-insensitively except where noted; matched text is never
# returned, only the code.
_CHECKS: list[tuple[str, str, list[re.Pattern]]] = [
    (
        "secret_detected",
        "HTML contains a secret or token value; remove it and fetch data at view time via apiFetch instead",
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
        [
            re.compile(r"\btokenUrl\b"),
            re.compile(r"\btoken_url\b"),
            re.compile(r"https?://[^\s'\"<>]*?/token(?:[/?#'\"<>]|$)", re.IGNORECASE),
        ],
    ),
    (
        "direct_fetch",
        "Live data must go via window.agentdocs.apiFetch, never a direct fetch, XMLHttpRequest, axios, or WebSocket call",
        [
            re.compile(r"\bfetch\s*\("),
            re.compile(r"\bXMLHttpRequest\b"),
            re.compile(r"\baxios\s*\.\s*(?:get|post|put|delete|request)\s*\("),
            re.compile(r"\bWebSocket\s*\("),
        ],
    ),
    (
        "external_resource",
        "Documents must be self-contained; no external scripts, stylesheets, images, iframes, media, form actions, CSS imports, or JS module imports",
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
            re.compile(r"@import\s+['\"]?https?:", re.IGNORECASE),
            re.compile(r"url\(\s*['\"]?https?:", re.IGNORECASE),
            re.compile(r"\bimport\s+(?:[^'\"]*?\sfrom\s+)?['\"]https?:", re.IGNORECASE),
        ],
    ),
    (
        "react_import",
        "Documents are plain HTML, never React/JSX",
        [
            re.compile(r"\bfrom\s+['\"]react(?:-dom)?['\"]"),
            re.compile(r"\bReact\s*\.\s*createElement\b"),
        ],
    ),
]


def validate_document_html(html: str) -> list[dict]:
    """Return a list of violations (empty = publishable).

    One entry per violated rule, never per match, and no matched
    text is included — a secret must not come back in the error.
    """
    violations: list[dict] = []
    for code, message, patterns in _CHECKS:
        if any(p.search(html) for p in patterns):
            violations.append({"code": code, "message": message})
    return violations
