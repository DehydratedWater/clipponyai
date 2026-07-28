# Screen observation and reflection

clipponyai can keep a structured, local timeline of foreground activity and periodically
review recent context. Screen observation is opt-in. Reflection is enabled by default but
is designed to return `SILENT` unless there is something useful or entertaining to say.

## Configuration

| Field | Default | Bounds | Meaning |
|---|---:|---:|---|
| `observation.enabled` | `false` | — | Starts the local foreground sampler. |
| `observation.sample_seconds` | `15` | 5–300 | Seconds between inexpensive OS metadata samples. |
| `observation.capture_window_titles` | `true` | — | Store the active window title when the platform exposes it. |
| `observation.idle_threshold_seconds` | `180` | 30–3600 | Input-idle time after which an episode is categorized as idle. |
| `observation.retention_days` | `14` | 1–365 | Age cutoff for observation rows. |
| `observation.max_rows` | `20000` | 500–100000 | Independent safety cap; keep this above roughly `600 × retention_days`. |
| `observation.redact_patterns` | `[]` | valid regexes | Regexes removed from window titles before storage, in order. |
| `reflection.enabled` | `true` | — | Periodically run a grounded, tool-using reflection turn. |
| `reflection.interval_minutes` | `20` | 5–240 | How often the pony considers reflecting. |
| `reflection.min_gap_minutes` | `60` | 15–480 | Minimum time between spoken reflections. |
| `reflection.quiet_after_nudge_minutes` | `10` | 0–120 | Do not reflect immediately after another proactive message. |
| `reflection.context_hours` | `3` | 1–24 | Observation history supplied to a reflection. |
| `reflection.max_tool_rounds` | `4` | 1–10 | Config-only cost bound for tool calls during one reflection. |

Example:

```yaml
observation:
  enabled: true
  sample_seconds: 15
  capture_window_titles: true
  idle_threshold_seconds: 180
  retention_days: 14
  max_rows: 20000
  redact_patterns:
    - '(?i)secret-project'
    - 'token=[^ ]+'

reflection:
  enabled: true
  interval_minutes: 20
  min_gap_minutes: 60
  quiet_after_nudge_minutes: 10
  context_hours: 3
  max_tool_rounds: 4
```

## Database schema

Observations live in `clippony.db`, returned by `clipponyai.config.db_path()`. On macOS this
is normally `~/Library/Application Support/clipponyai/clippony.db`; on Linux it is normally
under `~/.local/share/clipponyai/`.

```sql
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'os',
    app TEXT NOT NULL DEFAULT '',
    window_title TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'unknown',
    activity TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    idle_seconds INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.0,
    payload TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_observations_started_at ON observations(started_at);
```

`started_at` and `ended_at` bound an episode. `source` is `os` or `vision`. `app` and
`window_title` identify the foreground surface; `category` is one of `work`,
`communication`, `entertainment`, `browsing`, `learning`, `idle`, `other`, or `unknown`.
Vision samples add the neutral `activity` phrase, short `detail`, and model `confidence`.
OS rows record `idle_seconds` and use confidence `1.0`. `payload` is a JSON escape hatch for
future additive fields.

## Privacy, deletion, and retention

The log stores application names, optionally redacted window titles, idle time, categories,
and short vision-model descriptions. It never stores screenshot images. Screenshots used by
awareness are processed transiently and the existing screenshot privacy gate remains
separate.

To erase the timeline without affecting tasks, messages, or activity:

```sql
DELETE FROM observations;
```

Retention is enforced by both age and row count. Rows older than
`observation.retention_days` are removed, then the oldest rows beyond
`observation.max_rows` are removed. At volatile window-title workloads, budget roughly 600
episodes per day; if `doctor` reports an oldest row much younger than the configured age
cutoff, raise `max_rows`.

## macOS permissions

| Feature | Permission |
|---|---|
| Foreground application name | None |
| Window titles | Screen Recording |
| Awareness screenshots | Screen Recording |
| Cursor chase after a spoken nudge | Accessibility |

After granting a permission in System Settings → Privacy & Security, relaunch clipponyai.

## Analysis queries

```sql
-- Time per application, last 7 days
SELECT app,
       SUM(strftime('%s', ended_at) - strftime('%s', started_at)) / 60 AS minutes
FROM observations
WHERE source = 'os' AND category != 'idle' AND started_at >= datetime('now', '-7 days')
GROUP BY app
ORDER BY minutes DESC;

-- Time per category per day
SELECT date(started_at) AS day, category,
       SUM(strftime('%s', ended_at) - strftime('%s', started_at)) / 60 AS minutes
FROM observations
WHERE source = 'os' AND started_at >= datetime('now', '-14 days')
GROUP BY day, category
ORDER BY day DESC, minutes DESC;

-- Context switching: episodes per hour (a focus proxy)
SELECT strftime('%Y-%m-%d %H:00', started_at) AS hour, COUNT(*) AS switches
FROM observations
WHERE source = 'os' AND category != 'idle'
GROUP BY hour
ORDER BY hour DESC
LIMIT 48;

-- Longest uninterrupted focus blocks
SELECT started_at, app, window_title,
       (strftime('%s', ended_at) - strftime('%s', started_at)) / 60 AS minutes
FROM observations
WHERE source = 'os' AND category != 'idle'
ORDER BY minutes DESC
LIMIT 20;

-- Vision descriptions alongside the foreground application
SELECT v.started_at, v.category, v.activity, v.detail,
       (SELECT o.app FROM observations o
         WHERE o.source = 'os' AND o.started_at <= v.started_at
         ORDER BY o.started_at DESC LIMIT 1) AS focused_app
FROM observations v
WHERE v.source = 'vision'
ORDER BY v.started_at DESC
LIMIT 30;

-- Reflection and awareness speech/failure audit
SELECT at, action, detail FROM activity_log
WHERE action IN ('reflection_spoke', 'reflection_failed', 'awareness_intervention')
ORDER BY id DESC LIMIT 30;
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Window titles are empty | Confirm `capture_window_titles: true`, grant macOS Screen Recording, and relaunch. `clipponyai doctor` detects this case. |
| No observation rows appear | Set `observation.enabled: true`; in Settings, Apply starts or stops the recorder without a restart. |
| Reflection never speaks | This is often healthy. Check `reflection.enabled`, quiet hours, “don't bother me” silence, and `doctor`'s last-run value. It deliberately stays silent without new substance. |
| Reflection speaks too much | Raise `reflection.min_gap_minutes` or `quiet_after_nudge_minutes`, or disable reflection. Spoken output is still hard-gated by the minimum gap. |
