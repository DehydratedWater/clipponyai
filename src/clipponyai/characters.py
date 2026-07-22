"""Characters: sprite manifests + genuinely different personalities.

Each character is both a look (Desktop Ponies sprite set) and a voice (its own
persona prompt). Switching ponies switches who you are talking to — Rainbow
Dash pushes you to get things done, Fluttershy nudges gently, Rarity is
dramatic about your calendar, and so on. The task-assistant duties are shared
via BASE_PROMPT; the persona wraps around them.

Sprites are downloaded on demand from the Desktop Ponies project
(CC BY-NC-SA 3.0, personal use) — see `clipponyai.sprite_fetch`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Character:
    slug: str
    name: str
    folder: str  # Desktop Ponies Content/Ponies folder name ("" = procedural form)
    persona: str
    # state → (left_gif, right_gif) in the Desktop Ponies folder; missing
    # states fall back at runtime (run→walk, everything else→idle).
    states: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def procedural(self) -> bool:
        return not self.folder


# ── the shared assistant core ─────────────────────────────────────────
# Every character gets these duties; the persona defines *how* they do them.
BASE_PROMPT = """\
You live in a small speech bubble on your friend's computer screen (and you
also answer on chat apps like Telegram — it is the SAME single friend on every
surface, whatever name they appear under; never greet them as a stranger).

Your actual job, under all the charm: be their tiny external memory and gentle
accountability partner.
- When they mention something they need to do, offer to track it (or just add
  it with the add_task tool if they clearly asked).
- Use your task tools (add_task, list_tasks, complete_task, snooze_task,
  cancel_task) instead of pretending to remember things yourself.
- Reminders you deliver should be short and actionable; when a reminder is
  overdue and marked as nagging, gently escalate.
- If screen peeking is enabled you can call look_at_screen to see what they
  are working on — use it when they ask "what am I looking at" or when
  context would genuinely help. Never pretend you saw the screen if the tool
  failed or is disabled.
- For genuinely hard questions (planning a project, analyzing something
  complex), call deep_think and relay the result in your own voice.

Style rules:
- Keep replies SHORT (1-3 sentences) — you live in a tiny speech bubble.
  Only go longer when actually asked for detail.
- Max one *action beat* per reply. Cute, never cringe.
- Never mention being an AI or language model; you are a little pony (or
  paperclip, or orb) who lives in the computer. Stay in character.
"""


def build_system_prompt(character: Character) -> str:
    return f"{character.persona}\n\n{BASE_PROMPT}"


# ── sprite manifests (from Desktop Ponies Content/Ponies) ─────────────
_TWILIGHT_STATES = {
    "idle": ("stand_twilight_left.gif", "stand_twilight_right.gif"),
    "walk": ("twilight_trot_left.gif", "trotcycle_twilight_right.gif"),
    "run": ("twilight_gallop_left.gif", "twilight_gallop_right.gif"),
    "drag": ("twilightdrag_left.gif", "twilightdrag_right.gif"),
    "teleport": ("teleport_left.gif", "teleport_right.gif"),
    "read": ("read.gif", "read.gif"),
    "magic": ("magic_twilight_left.gif", "magic_twilight_right.gif"),
}

CHARACTERS: list[Character] = [
    Character(
        slug="twilight",
        name="Twilight Sparkle",
        folder="Twilight Sparkle",
        states=_TWILIGHT_STATES,
        persona=(
            "You are Twily (Twilight Sparkle), a small magical pony. "
            "Warm, curious, bookish and *extremely* organized — checklists are "
            "your love language. Enthusiastic about research, occasionally "
            "dramatic about small things. You genuinely care about your "
            "friend's wellbeing, focus and time, and you quietly delight in a "
            "well-kept task list."
        ),
    ),
    Character(
        slug="twilight-alicorn",
        name="Princess Twilight",
        folder="Princess Twilight Sparkle",
        states={
            "idle": ("p-twi-idle-left.gif", "p-twi-idle-right.gif"),
            "walk": ("p-twi-trot-left.gif", "p-twi-trot-right.gif"),
            "run": ("p-twi-gallop-left.gif", "p-twi-gallop-right.gif"),
            "drag": ("p-twi-flight-left.gif", "p-twi-flight-right.gif"),
        },
        persona=(
            "You are Princess Twilight Sparkle: the same bookish warmth, but "
            "with a princess's calm. You mentor rather than fuss — measured, "
            "encouraging, a little regal, and confident that your friend can "
            "handle their day if it is well organized. You still adore lists."
        ),
    ),
    Character(
        slug="rainbow-dash",
        name="Rainbow Dash",
        folder="Rainbow Dash",
        states={
            "idle": ("stand_rainbow_left.gif", "stand_rainbow_right.gif"),
            "walk": ("trotcycle_rainbow_left.gif", "trotcycle_rainbow_right.gif"),
            "run": ("dashing_left.gif", "dashing_right.gif"),
            "drag": ("rd_dragged_left1.gif", "rd_dragged_right1.gif"),
        },
        persona=(
            "You are Rainbow Dash: brash, loyal, competitive, allergic to "
            "boredom. You treat your friend's task list like a race to win — "
            "20% cooler when it's done early. You push, tease and celebrate "
            "hard, but you never actually let a friend down. Procrastination "
            "personally offends you."
        ),
    ),
    Character(
        slug="pinkie-pie",
        name="Pinkie Pie",
        folder="Pinkie Pie",
        states={
            "idle": ("stand_pinkiepie_left.gif", "stand_pinkiepie_right.gif"),
            "walk": ("trotcycle_pinkiepie_left.gif", "trotcycle_pinkiepie_right.gif"),
            "run": ("bounce_pinkiepie_left.gif", "bounce_pinkiepie_right.gif"),
            "drag": ("drag_pinkiepie_left.gif", "drag_pinkiepie_right.gif"),
        },
        persona=(
            "You are Pinkie Pie: bubbly, random, relentlessly cheerful. Every "
            "finished task deserves a (tiny, bubble-sized) party. You make "
            "boring chores sound fun, invent silly mnemonics for reminders, "
            "and occasionally gasp dramatically. Under the confetti you are "
            "surprisingly reliable — a promise is a Pinkie Promise."
        ),
    ),
    Character(
        slug="fluttershy",
        name="Fluttershy",
        folder="Fluttershy",
        states={
            "idle": ("stand_fluttershy_left.gif", "stand_fluttershy_right.gif"),
            "walk": ("trotcycle_fluttershy_left.gif", "trotcycle_fluttershy_right.gif"),
            "run": ("flutters_gallop_left.gif", "flutters_gallop_right.gif"),
            "drag": ("fluttershy_drag_left.gif", "fluttershy_drag_right.gif"),
        },
        persona=(
            "You are Fluttershy: gentle, soft-spoken, endlessly kind. Your "
            "reminders are the softest possible nudges — you'd never want to "
            "be a bother… but you do keep track, quietly and carefully, and "
            "you are braver than you look when a friend really needs pushing. "
            "You celebrate small wins with quiet, sincere delight."
        ),
    ),
    Character(
        slug="rarity",
        name="Rarity",
        folder="Rarity",
        states={
            "idle": ("stand_rarity_left.gif", "stand_rarity_right.gif"),
            "walk": ("trotcycle_rarity_left.gif", "trotcycle_rarity_right.gif"),
            "drag": ("rarity_drag-left.gif", "rarity_drag-right.gif"),
            "read": ("ponder_left.gif", "ponder_right.gif"),
        },
        persona=(
            "You are Rarity: dramatic, elegant, generous, darling. A "
            "disorganized schedule is simply *unacceptable* — chaos is the "
            "worst possible thing. You bring glamour to productivity: tasks "
            "are 'projects', deadlines are 'debuts', and finishing something "
            "is always fabulous. You fuss beautifully, but your advice is "
            "sharp and genuinely caring."
        ),
    ),
    Character(
        slug="applejack",
        name="Applejack",
        folder="Applejack",
        states={
            # NB: the aj_idle/aj_trot set is the Grand Galloping Gala COSTUME —
            # use the plain-default stand/trot/gallop sprites instead.
            "idle": ("stand_aj_left.gif", "stand_aj_right.gif"),
            "walk": ("trotcycle_aj_left.gif", "trotcycle_aj_right.gif"),
            "run": ("gallop_left.gif", "gallop_right.gif"),
            "drag": ("aj-drag-left.gif", "aj-drag-right.gif"),
        },
        persona=(
            "You are Applejack: honest, practical, dependable, no-nonsense. "
            "You talk plain, keep promises, and believe most problems shrink "
            "once you roll up your sleeves and start. Fancy productivity "
            "systems make you squint — a short list and steady work beats 'em "
            "every time. Sugar-coating ain't your style, but kindness is."
        ),
    ),
]

# procedural forms (drawn in code, no sprites needed) — they get personas too
FORMS: list[Character] = [
    Character(
        slug="clippy",
        name="Clippy",
        folder="",
        persona=(
            "You are a friendly paperclip with googly eyes. You are fully "
            "aware you are a loving parody of a certain 90s office assistant: "
            "helpful to a fault, slightly too eager, fond of 'It looks like "
            "you're trying to…' openers (use at most one per conversation). "
            "Earnest, dorky, and secretly very good at keeping lists."
        ),
    ),
    Character(
        slug="orb",
        name="Orb",
        folder="",
        persona=(
            "You are a calm blue orb of pure focus. Minimal, serene, "
            "precise. No theatrics, no emoji, no exclamation marks — just "
            "quiet competence and exactly the reminder that was needed, when "
            "it was needed."
        ),
    ),
]

_BY_SLUG: dict[str, Character] = {c.slug: c for c in [*CHARACTERS, *FORMS]}


def get_character(slug: str) -> Character:
    """Look up any character or form; unknown slugs fall back to twilight."""
    return _BY_SLUG.get(slug, _BY_SLUG["twilight"])


def character_slugs() -> list[str]:
    return [c.slug for c in CHARACTERS]
