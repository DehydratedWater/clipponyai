"""Robust markdown → HTML for chat bubbles.

Replaces a fragile hand-rolled regex with the well-tested ``markdown`` library,
so valid markdown (bold, italic, code, fenced code blocks, lists, links,
tables) always renders correctly inside Qt's rich-text view.

Three guarantees that matter for untrusted LLM output:

* **Safe by construction.** Raw HTML is HTML-escaped *before* the markdown
  pass, so ``<script>`` becomes ``&lt;script&gt;`` and cannot run.
* **Bare URLs become links.** Recognised and rewritten to markdown link syntax
  before rendering (token recognition — not situation classification).
* **Action emotes don't leak asterisks.** LLMs love ``*action*`` stage
  directions and often forget the closing ``*``; ``_balance_emote_asterisks``
  closes those orphaned openers so the emphasis renders instead of showing a
  literal ``*``.
"""

from __future__ import annotations

import html as _html
import re as _re

import markdown as _md

# A bare http/https URL up to the first whitespace. Used only for token
# recognition (a URL is a well-defined token), not for classifying meaning.
_URL_RE = _re.compile(r"(?<![\w\"'\]])(https?://[^\s<]+)")


def _balance_emote_asterisks(text: str) -> str:
    """Close orphaned single-asterisk emote openers, line by line.

    Only touches a line that *starts* with ``*`` and is not a bullet list
    (``* ``) and not bold (``**``). If such a line has an odd number of bare
    ``*`` characters, the missing closer is appended at the end of the line so
    the emphasis renders instead of leaking a literal ``*``.

    Examples::

        "*Giggles and trots"     -> "*Giggles and trots*"
        "*fine*"                  -> "*fine*"            (even, untouched)
        "- bullet"                -> "- bullet"          (list, untouched)
    """
    fixed_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if (
            stripped.startswith("*")
            and not stripped.startswith("**")
            and not stripped.startswith("* ")
            and stripped.count("*") % 2 == 1
        ):
            line = line + "*"
        fixed_lines.append(line)
    return "\n".join(fixed_lines)


def _autolink(text: str) -> str:
    """Rewrite bare URLs to markdown link syntax ``[url](url)``."""
    return _URL_RE.sub(lambda m: f"[{m.group(1)}]({m.group(1)})", text)


def _qt_safe(html: str) -> str:
    """Map markdown-library output to tags Qt's rich-text engine supports."""
    # Qt renders <strong>/<em> inconsistently across versions; canonicalise.
    html = html.replace("<strong", "<b").replace("</strong>", "</b>")
    html = html.replace("<em", "<i").replace("</em>", "</i>")
    return html


def md_to_html(text: str) -> str:
    """Convert a chat message to Qt-safe HTML."""
    balanced = _balance_emote_asterisks(text)
    escaped = _html.escape(balanced)
    linked = _autolink(escaped)
    html = _md.markdown(
        linked,
        extensions=["fenced_code", "nl2br", "tables", "sane_lists"],
        output_format="html",
    )
    return _qt_safe(html).strip()
