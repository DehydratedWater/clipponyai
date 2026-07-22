# 🦄 clipponyai

A cute desktop pony who is secretly a competent personal assistant.

She trots around your screen, follows your cursor when something really needs
your attention, keeps track of your tasks, notices when you promise things in
passing ("I'll call mom later") and reminds you until you actually do them —
politely at first, then with escalating pony determination. You can talk to
her on your desktop or from your phone via Telegram; it's the same one
conversation either way.

Powered by **any LLM provider you like** — hosted APIs if you don't own a GPU
farm, or your own local models if you do.

```
pip install clipponyai        # + PySide6 GUI, included
clipponyai init               # writes ~/.config/clipponyai/config.yaml
export OPENAI_API_KEY=sk-…    # or any other provider, see below
clipponyai                    # 🦄 (sprites download on first run)
```

## What she does

- **Tracks tasks & reminds you.** Tell her "remind me to submit the report
  tomorrow at 10" — or just mention "I should water the plants" and she'll
  quietly start tracking it. Reminders escalate on a fixed cadence (30 min,
  1 h, 2 h, 4 h, 6 h…), cap at 8 pings, then she gives up with a little
  gravestone notice (say "restore …" to revive). Quiet hours are respected.
- **Chases your cursor** for reminders that must not be missed — a corner
  notification is easy to ignore; a pony galloping at your pointer is not.
  Click her (or the bubble) to acknowledge.
- **Sees your screen — only if you let her.** Screen peeking is **off by
  default**; enable it in the right-click menu and she can answer "what am I
  looking at?" with a vision model.
- **Talks from your phone.** Enable Telegram, and reminders reach you there
  too. The channel layer is a small base class — other messengers are one
  subclass away.
- **Eight personalities.** Each character is a different prompt, not just a
  different sprite: Twilight organizes, Rainbow Dash races you, Fluttershy
  nudges gently, Rarity is dramatic about your calendar, Applejack talks
  plain, Pinkie throws bubble-sized parties, plus Clippy (📎, fully aware
  what he is) and a minimal focus Orb.

## LLM providers — bring your own brain

Everything OpenAI-compatible works. The config ships with ready entries —
pick one with `llm.active`, export the key, done:

| provider | key env var | note |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | default |
| `anthropic` | `ANTHROPIC_API_KEY` | via Anthropic's OpenAI-compat endpoint |
| `openrouter` | `OPENROUTER_API_KEY` | one key, hundreds of models |
| `groq` | `GROQ_API_KEY` | very fast, free tier |
| `ollama` | — | local models, no key, no cloud |

Each provider names three models: `fast_model` (chat turns + the small
"sensor" calls), `slow_model` (the `deep_think` escalation lane) and
`vision_model` (screenshots). That's the **fast–slow architecture** from
[open-agent-compiler](https://pypi.org/project/open-agent-compiler/), which
this project uses as its LLM layer: snappy small-model turns by default, a
stronger model only when the question deserves it. Add your own entry with
any `base_url` (vLLM, llama.cpp, LM Studio, a proxy…) — and switch brains at
runtime from the right-click menu.

```yaml
llm:
  active: ollama          # ← switch here, or from the 🧠 menu
  providers:
    my-vllm:
      base_url: http://192.168.0.42:8000/v1
      fast_model: Qwen/Qwen3-8B
      slow_model: Qwen/Qwen3-32B
      extra_body: {chat_template_kwargs: {enable_thinking: false}}
```

## How the assistant part works (design notes)

The task logic is distilled from a much larger personal-assistant
orchestrator, keeping its battle-tested rules:

- **Small fast LLM calls over regexes.** Understanding language — "did they
  just say they finished something?", "what time is 'jutro wieczorem'?" — is
  done by tiny fast-model calls with deliberately small context (a few dozen
  tokens), so it's cheap, accurate and works in any language.
- **LLM output is never ground truth.** Every state change is grounded
  against real database rows first: proposed task ids must exist, proposed
  matches must share actual words with the message, ambiguity becomes a
  question instead of a guess.
- **Listings are code, not conversation.** `/tasks` renders the database
  verbatim. If the list is long, the list is long.
- **Reminders are deterministic.** Nudge messages come from fixed templates
  on a fixed cadence — a reminder must never hallucinate.
- Every status change lands in an audit log table.

## Configuration

Everything lives in one commented YAML (`clipponyai init` creates it;
`clipponyai doctor` checks it):

```yaml
ui:
  character: twilight       # twilight-alicorn, rainbow-dash, pinkie-pie,
                            # fluttershy, rarity, applejack, clippy, orb
  scale: 1.0
  idle_wander: true         # random walks, reading, little quips
  attention_seconds: 30     # how long a reminder chases your cursor
screenshot_enabled: false   # privacy: SHE CANNOT SEE YOUR SCREEN unless true
auto_track_commitments: true  # notice "I'll do X" promises automatically
reminders:
  quiet_hours_start: 23     # no pings at night
  quiet_hours_end: 8
  nudge_gaps_minutes: [30, 60, 120, 240, 360]
  max_nudges: 8
telegram:
  enabled: false
  token_env: TELEGRAM_BOT_TOKEN     # token from @BotFather
  allowed_user_ids: []              # EMPTY = answers nobody. add your id!
```

Telegram setup: `pip install 'clipponyai[telegram]'`, create a bot with
[@BotFather](https://t.me/BotFather), export the token, put your numeric user
id in `allowed_user_ids` (ask [@userinfobot](https://t.me/userinfobot)), set
`enabled: true`. On a server, run `clipponyai --headless`.

## CLI

```
clipponyai            run the pony (default)
clipponyai --headless run without GUI (telegram + reminders only)
clipponyai init       write the default config
clipponyai doctor     check config / keys / sprites / extras
clipponyai tasks      print the task overview in your terminal
clipponyai fetch-sprites   (re)download sprites
```

## Platforms

Pure Python + Qt: Linux, Windows and macOS all work for the overlay, bubble,
cursor-chasing and screenshots (`mss`). Most tested on Linux/X11. On Wayland,
window self-positioning is compositor-dependent — the pony works but may
wander less precisely.

## Licensing

- **Code: MIT.**
- **Sprites: not included.** They are downloaded on first run from the
  wonderful [Desktop Ponies](https://github.com/RoosterDragon/Desktop-Ponies)
  project (CC BY-NC-SA 3.0, personal use) into your local data directory,
  with an attribution notice written alongside. Thank you, Desktop Ponies
  artists! If you fork this for anything commercial, ship it with the
  built-in 📎/🔵 procedural characters and leave the ponies out.

## Development

```
uv venv && uv pip install -e '.[dev,telegram]'
uv run pytest
```

Tests cover the task store, nudge cadence, time parsing, config, sensors and
the brain's tool loop (with a fake LLM client — no network needed).
