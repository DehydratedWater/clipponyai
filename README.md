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
clipponyai check-llm          # verify your provider is reachable
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
- **Proactive focus awareness — opt-in, double-gated.** When both
  `screenshot_enabled` and `awareness.enabled` are `true`, the pony periodically
  classifies your screen via the LLM vision lane and can interrupt you for
  distractions (social media during work hours) or after-hours work. Delivered
  via cursor-chase nudge. Disabled by default; requires a vision-capable model.
- **Talks from your phone.** Enable Telegram, and reminders reach you there
  too. The channel layer is a small base class — other messengers are one
  subclass away.
- **Eight personalities.** Each character is a different prompt, not just a
  different sprite: Twilight organizes, Rainbow Dash races you, Fluttershy
  nudges gently, Rarity is dramatic about your calendar, Applejack talks
  plain, Pinkie throws bubble-sized parties, plus Clippy (📎, fully aware
  what he is) and a minimal focus Orb.

## Planner, routines, goals, and accountability

The pony uses four related but distinct kinds of planning data. You can manage
all of them by talking naturally or through **📊 Planner & Activity** in the
pony/tray menu:

- **Tasks** are one-time agenda items: “submit the form tomorrow at 10.”
- **Routines** recur daily, on selected weekdays, or monthly. Examples include
  breakfast every day, medication at 08:00, watering flowers every Tuesday,
  and paying tax on day 20 of each month. Monthly days that do not exist in a
  short month are clamped to that month's final day. Each occurrence can be
  done, skipped, or missed and contributes to current/longest streaks.
- **Goals** are outcomes with count or streak conditions. They can be checked
  in manually or linked to routines, such as “read for 30 consecutive days.”
- **Rules** describe accountability boundaries, such as “no YouTube after
  22:00.” Time rules are deterministic. Screen-based rules only run when the
  separate, opt-in screen-awareness privacy gates are enabled.

The dashboard has seven live tabs: **Tasks, Routines, Goals, Rules, Activity,
Observations, and Token Usage**. Forms and conversation tools write to the same SQLite records,
so changes made in one surface immediately appear in the other.

On a fresh profile, the pony asks one compact onboarding batch about work
hours, recurring responsibilities, goals, and boundaries. You may answer only
part of it or skip it. Later context-gap questions have a default four-hour
minimum and only fire when useful context is missing. They stay silent when
there is a pending agenda, a due routine/goal check-in, quiet/off-hours, another
notification in that scheduler pass, or an explicit “don't bother me” period.
These controls are editable in **Settings → Proactive**.

For transparency, **Activity** retains the last 200 pony actions, including
awareness interventions and sensor failures. Structured screen samples live in
the separate, independently retained **Observations** tab; neither surface stores
screenshots. **Token Usage**
shows chat/sensor/slow/vision consumption for today, seven days, and all time;
entries are marked *estimated* when a provider does not return exact usage.

Conversation examples:

- “Remind me to take medication every day at 8 AM.”
- “Water the flowers every Tuesday at 18:00.”
- “Pay tax on the 20th of every month, due by 17:00.”
- “Create a 14-day breakfast streak goal and link it to my breakfast routine.”
- “No YouTube after 10 PM, and don't ask me setup questions for 24 hours.”

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
| `qwen27b-vllm` | — | local Qwen3.5-27B via vLLM on `127.0.0.1:8082` |

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

### `qwen27b-vllm` — local Qwen3.5-27B out of the box

The `qwen27b-vllm` preset targets a vLLM server running
`cyankiwi/Qwen3.5-27B-AWQ-BF16-INT8` on `http://127.0.0.1:8082/v1`. It needs
no API key and sets `enable_thinking: false` so the model returns normal text
instead of reasoning tokens. This model supports both text and image inputs
(multimodal), so screenshots and awareness work out of the box.
Point your own vLLM instance there or edit the `base_url` in config to match your setup.

After starting vLLM, verify connectivity with:

```
clipponyai check-llm
clipponyai check-llm --vision    # verify image understanding
```

### Local model capabilities

The `qwen27b-vllm` preset (Qwen3.5-27B) supports images natively — screenshots
and proactive awareness work without a separate vision model. Other local
models may be text-only; set a dedicated `vision_model` (e.g. `qwen2.5vl:7b`
on Ollama) or use a hosted provider with built-in vision (OpenAI, Anthropic,
OpenRouter) for image understanding.

`clipponyai doctor` reports vision capability. Run `clipponyai check-llm --vision`
to verify image understanding end-to-end.

### `clipponyai check-llm`

Smoke-test your active provider before launching the pony:

```
clipponyai check-llm
```

Sends a minimal chat turn and prints `ok — <provider> (<model>) replied: …`
on success (exit 0) or `ERROR: …` on failure (exit 1). Useful in CI,
docker-entrypoint scripts, or just to confirm your local server is running.

With `--vision`, sends a synthetic two-color image and verifies the model can
read it. Returns 0 only when the model correctly identifies both colors.

```
clipponyai check-llm --vision
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

Everything lives in one YAML file (`clipponyai init` creates it;
`clipponyai doctor` checks it):

```yaml
ui:
  character: twilight       # twilight-alicorn, rainbow-dash, pinkie-pie,
                            # fluttershy, rarity, applejack, clippy, orb
  scale: 1.0
  idle_wander: true         # random walks, reading, little quips
  attention_seconds: 30     # how long a reminder chases your cursor
screenshot_enabled: false   # privacy: SHE CANNOT SEE YOUR SCREEN unless true
awareness:
  enabled: false              # proactive focus/distraction awareness
  interval_seconds: 300       # how often to check (30–3600s)
  cooldown_minutes: 30        # silence after alert (5–480m)
  minimum_confidence: 0.7     # skip low-confidence assessments
  focus_policy: "During work hours, interrupt if the user is on social media …"
auto_track_commitments: true  # notice "I'll do X" promises automatically
reminders:
  quiet_hours_start: 23     # no pings at night
  quiet_hours_end: 8
  nudge_gaps_minutes: [30, 60, 120, 240, 360]
  max_nudges: 8
  work_hours:
    enabled: false           # set true to activate work-hour boundaries
    start: "09:00"           # workday start (HH:MM)
    end: "17:00"             # workday end (HH:MM)
    weekdays: [0, 1, 2, 3, 4]  # Mon=0 .. Sun=6
    closing_nudge: true      # list pending tasks at end of workday
    suppress_off_hours: false  # silence ordinary reminders outside work hours
logwatch:
  enabled: false             # privacy-gated: disabled by default
  files: []                  # explicit absolute paths only
  max_lines_per_file: 200
  max_total_chars: 8000
telegram:
  enabled: false
  token_env: TELEGRAM_BOT_TOKEN     # token from @BotFather
  allowed_user_ids: []              # EMPTY = answers nobody. add your id!
```

### Personalize your pony (MCP + Skills)

Connect any [Model Context Protocol](https://modelcontextprotocol.io/) server
in `config.yaml` using paste-friendly `mcpServers`-style fields. After a
restart, its tools appear automatically in chat; run `clipponyai check-mcp`
first to verify each connection and see the discovered tool names. Per-server
allow and deny lists keep the pony's tool set focused.

Agent Skills are portable instruction packs: put a folder containing
`SKILL.md` in a scanned skills directory and the pony will advertise its name
and description, then load the full instructions only when relevant. The
format follows the open [Agent Skills specification](https://agentskills.io/).

See [Personalization with MCP and Agent Skills](docs/personalization.md) for
the complete config reference, runnable examples, troubleshooting, skill
authoring, and a commented config template.

### Work hours

Work hours let the pony emit a **closing nudge** at the end of each workday,
listing all still-pending tasks. It fires once per workday (persisted in
SQLite so it never duplicates) and respects quiet hours.

```yaml
reminders:
  work_hours:
    enabled: true
    start: "09:00"
    end: "17:00"
    weekdays: [0, 1, 2, 3, 4]   # Mon–Fri
    closing_nudge: true
```

Set `suppress_off_hours: true` to silence all ordinary reminders outside the
configured work window (the pony still drops exhausted tasks).

### Proactive focus awareness

Proactive awareness lets the pony periodically classify your screen and
interrupt you when it detects a distraction (e.g. social media during work
hours) or after-hours work. It requires **two independent gates**, both must
be `true`:

1. `screenshot_enabled: true` — the pony must be allowed to capture the screen
2. `awareness.enabled: true` — the user must explicitly opt in to proactive scanning

```yaml
screenshot_enabled: true
awareness:
  enabled: true
  interval_seconds: 300       # seconds between checks (30–3600)
  cooldown_minutes: 30        # silence after each alert (5–480)
  minimum_confidence: 0.7     # skip assessments below this threshold (0.0–1.0)
  focus_policy: "During work hours, interrupt on social media. After hours, remind if working."
```

**How it works.** Every `interval_seconds`, the pony takes a screenshot and
sends it to the LLM's vision lane along with the current local time and weekday,
the current work-hours status, pending tasks overview, and the natural-language
`focus_policy`. The vision model returns a structured decision
(`should_interrupt`, `confidence`, `reason`). If the decision passes the
`minimum_confidence` threshold and says to interrupt, the pony delivers a
cursor-chase nudge. A cooldown timer (persisted in SQLite) prevents repeat
alerts within `cooldown_minutes`.

**Time-conditional policies need work hours enabled.** The default
`focus_policy` gates its no-social-media clause on "during work hours". The pony
only knows your work-hours window when `reminders.work_hours.enabled: true` —
otherwise it is told the condition is unverified, and clauses conditioned on it
do not fire. Set your window if you want that gating to be sharp:

```yaml
reminders:
  work_hours:
    enabled: true
    start: "09:00"
    end: "17:00"
    weekdays: [0, 1, 2, 3, 4]   # Mon=0 … Sun=6
```

**macOS Screen Recording permission.** On macOS, `screenshot_enabled: true`
requires the Screen Recording permission grant in
**System Settings → Privacy & Security → Screen Recording**. After granting,
relaunch the app. Without this permission, screenshots return blank images and
awareness cannot function.

`clipponyai doctor` reports awareness state, privacy gates, interval, cooldown,
and the resolved vision model. Use `clipponyai check-llm --vision` to verify the
complete local image path.

### What she notices

Screen observation is a separate, opt-in local timeline. It records the foreground
application, an optionally redacted window title, category, idle time, and the vision
model's short activity phrase. It never stores screenshots. Rows default to 14 days of
retention with a 20,000-row safety cap and appear in the dashboard's **Observations** tab.
The `recent_screen_activity` chat tool lets the pony answer questions such as “what was I
doing this afternoon?” from that same structured record.

```yaml
observation:
  enabled: true
  sample_seconds: 15
  capture_window_titles: true
  idle_threshold_seconds: 180
  retention_days: 14
  max_rows: 20000
  redact_patterns:
    - '(?i)private-project'
```

On macOS, application names and idle time need no permission. Window titles and awareness
screenshots need **Screen Recording**; cursor chasing needs **Accessibility**. Turn the
feature off with `observation.enabled: false`, or configure one regex per line under
`redact_patterns` to remove sensitive title fragments before storage. See
[Screen observation and reflection](docs/observation.md) for the schema and analysis SQL.

### Reflection

Reflection lets the pony review recent grounded context roughly every 20 minutes and use
her normal tools when an insight needs more information. She stays silent by default,
speaks at most once an hour with the default settings, honors quiet hours, and respects the
existing “don't bother me” mute. Observation can remain off; reflection then falls back to
tasks, routines, goals, recent activity, messages, and available tools.

```yaml
reflection:
  enabled: true
  interval_minutes: 20
  min_gap_minutes: 60
  quiet_after_nudge_minutes: 10
  context_hours: 3
```

### Privacy-gated log watching

Logwatch lets the pony tail local log files and answer questions about them
via the FAST LLM lane. It is **disabled by default** and only reads the last
N lines of explicitly configured absolute paths — no regex, no streaming, no
service or database access.

```yaml
logwatch:
  enabled: true
  files:
    - /var/log/myapp/error.log
    - /home/user/.local/share/myapp/output.log
  max_lines_per_file: 200
  max_total_chars: 8000
```

`clipponyai doctor` reports logwatch state, file count, and whether each
configured path exists.

### In-app settings dialog

Right-click the pony to open the settings dialog. It covers every config
section in a tabbed UI:

- **Privacy** — screen peeking toggle, auto-commitment tracking
- **Pony** — character, size, attention chase duration, idle wandering
- **Reminders** — enabled, check interval, quiet hours, nudge gaps, max nudges, batch limit
- **Work Hours** — enable/disable, start/end times, active weekdays, closing nudge, off-hours suppression
- **Log Watch** — enable/disable, file paths, line/char limits
- **Awareness** — focus checks plus screen-observation consent, sampling, retention, title capture, and redaction
- **Proactive** — context-gap questions, onboarding, and periodic reflection controls
- **LLM** — switch active provider
- **Misc** — autostart on login

Changes validate on Apply and persist to `config.yaml` immediately. Some
changes (provider switch, character switch) need a restart to take full
effect.

Telegram setup: `pip install 'clipponyai[telegram]'`, create a bot with
[@BotFather](https://t.me/BotFather), export the token, put your numeric user
id in `allowed_user_ids` (ask [@userinfobot](https://t.me/userinfobot)), set
`enabled: true`. On a server, run `clipponyai --headless`.

## CLI

```
clipponyai              run the pony (default)
clipponyai --headless   run without GUI (telegram + reminders only)
clipponyai init         write the default config
clipponyai doctor       check config / keys / sprites / extras / awareness
clipponyai tasks        print the task overview in your terminal
clipponyai fetch-sprites   (re)download sprites
clipponyai check-llm    smoke-test the active LLM provider (returns 0/1)
clipponyai check-mcp    check MCP connections and discovered tools (returns 0/1)
clipponyai autostart [enable|disable|status]   manage login autostart
clipponyai install-desktop       install .desktop entry (Linux) / explain (macOS)
```

### `clipponyai doctor`

Reports a full health check including:

- Config file presence and provider/key status
- Local endpoint health-check suggestion (`check-llm`)
- Work-hours state (enabled/disabled, schedule, closing nudge)
- Logwatch privacy status and file path existence
- Autostart status (enabled/disabled with path)
- Platform-specific screen permission guidance (macOS Screen Recording + Accessibility)
- Vision model capability report (multimodal status)
- Proactive awareness state (off/on, screenshot gate, interval, cooldown)
- First-run next steps when sprites or config are missing

### `clipponyai autostart`

Enable, disable, or check login autostart:

```
clipponyai autostart status    # current state
clipponyai autostart enable    # install autostart entry
clipponyai autostart disable   # remove autostart entry
```

On Linux, writes to `~/.config/autostart/clipponyai.desktop`. On macOS,
writes to `~/Library/LaunchAgents/clipponyai.plist`. Both are user-level
only and idempotent.

### `clipponyai install-desktop`

On Linux, installs a `.desktop` file to `~/.local/share/applications/` so the
app appears in the application menu. On macOS, explains that LaunchAgents
handle app launch instead.

## Platforms

Pure Python + Qt: Linux, Windows and macOS all work for the overlay, bubble,
cursor-chasing and screenshots (`mss`). Most tested on Linux/X11. On Wayland,
window self-positioning is compositor-dependent — the pony works but may
wander less precisely.

**She never steals your keyboard.** Everything she starts herself — speaking,
a cursor-chase nudge, coming back from hidden — puts her on top without
activating the app, so a bubble appearing mid-keystroke can't swallow what
you were typing. (On macOS this needs care: Qt's `raise()` activates the
whole application there, so she uses AppKit's `orderFront:` instead. Her
always-on-top window level keeps her visible either way.)

### macOS permissions

macOS requires explicit user grants for two capabilities:

- **Screen Recording** — needed when `screenshot_enabled: true`. Grant in
  **System Settings → Privacy & Security → Screen Recording**. After granting,
  you may need to restart the app (macOS requires relaunch after permission
  changes). Required for both manual screen peeking and proactive awareness.
- **Accessibility** — needed for cursor-chasing (the pony follows your
  pointer). Grant in **System Settings → Privacy & Security → Accessibility**.

`clipponyai doctor` prints these instructions when screen peeking is enabled
on macOS.

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

Tests cover the task store, nudge cadence, time parsing, config, sensors,
the brain's tool loop (with a fake LLM client — no network needed), install
helpers (with platform and path mocking), and CLI commands.
