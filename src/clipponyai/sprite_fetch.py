"""Download Desktop Ponies sprites into the local data dir (no Qt needed).

Sprites are NOT bundled with this package: the Desktop Ponies project
distributes them under CC BY-NC-SA 3.0 (personal use), while this code is
MIT. Fetching them to your own machine on first run keeps the licenses
cleanly separated. See https://github.com/RoosterDragon/Desktop-Ponies —
thank you to the Desktop Ponies artists!
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from pathlib import Path

from .characters import CHARACTERS
from .config import sprites_dir

log = logging.getLogger("clipponyai.sprites")

BASE = "https://raw.githubusercontent.com/RoosterDragon/Desktop-Ponies/master/Content/Ponies"

ATTRIBUTION = """\
Sprites in this directory come from the Desktop Ponies project
(https://github.com/RoosterDragon/Desktop-Ponies) and are licensed
CC BY-NC-SA 3.0 (personal use): https://creativecommons.org/licenses/by-nc-sa/3.0/
They were downloaded by clipponyai and are not part of the clipponyai package.
"""


def have_sprites(out: Path | None = None) -> bool:
    """True when at least the default character's idle sprite exists."""
    out = out or sprites_dir()
    return (out / "twilight" / "idle_right.gif").exists()


def fetch_sprites(out: Path | None = None, progress=print) -> int:
    """Download every character's sprite set (skipping existing files).
    Returns the number of missing files afterwards (0 = complete)."""
    out = out or sprites_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / "ATTRIBUTION.txt").write_text(ATTRIBUTION)
    missing = 0
    for character in CHARACTERS:
        folder = urllib.parse.quote(character.folder)
        char_dir = out / character.slug
        char_dir.mkdir(exist_ok=True)
        for state, (left, right) in character.states.items():
            for facing, name in (("left", left), ("right", right)):
                dest = char_dir / f"{state}_{facing}.gif"
                if dest.exists():
                    continue
                url = f"{BASE}/{folder}/{urllib.parse.quote(name)}"
                try:
                    with urllib.request.urlopen(url, timeout=30) as response:
                        dest.write_bytes(response.read())
                    progress(f"  ✓ {character.slug}/{state}_{facing}.gif")
                except Exception as e:
                    missing += 1
                    progress(f"  ✗ {character.slug}/{state}_{facing}.gif — {e}")
    return missing
