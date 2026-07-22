"""Tiny markdown → HTML for chat bubbles (escape first, then inline bits)."""

from __future__ import annotations

import html
import re


def md_to_html(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"```(.*?)```", r"<pre>\1</pre>", t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', t)
    t = re.sub(r"^[-•] (.*)$", r"&nbsp;&nbsp;• \1", t, flags=re.M)
    return t.replace("\n", "<br>")
