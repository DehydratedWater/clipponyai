# Personalization with MCP and Agent Skills

clipponyai has two config-driven extension points:

- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) connects
  tools and data services.
- [Agent Skills](https://agentskills.io/) provide reusable instructions and
  reference material.

Both are generic. Adding a server or skill requires no changes to
clipponyai's source code.

## MCP servers

Add an `mcp` section to the top level of `config.yaml`. The entries under
`servers` use the same connection fields commonly shown under `mcpServers` in
MCP server READMEs, with clipponyai's filtering and timeout fields alongside
them.

### Complete config reference

| Field | Default | Meaning |
|---|---:|---|
| `mcp.enabled` | `false` | Global switch. No MCP process or connection starts while false. |
| `mcp.servers` | `{}` | Map of server names to server configs. Names may contain letters, numbers, `_`, and `-`. |
| `servers.<name>.type` | auto | Optional FastMCP transport override, such as `stdio`, `http`, or `sse`. Normally omit it and let the URL/command select the transport. |
| `servers.<name>.command` | — | Executable for a local stdio server. Set exactly one of `command` or `url`. |
| `servers.<name>.args` | `[]` | Arguments passed to `command`, in order. |
| `servers.<name>.env` | `{}` | Environment variables added to a local server process. `${ENV_VAR}` placeholders are expanded before connection. |
| `servers.<name>.cwd` | current directory | Working directory for a local server process. |
| `servers.<name>.url` | — | URL for a remote HTTP/SSE server. Set exactly one of `url` or `command`. |
| `servers.<name>.headers` | `{}` | HTTP headers for a remote server. Use `${ENV_VAR}` placeholders for secrets. |
| `servers.<name>.enabled` | `true` | Per-server switch. A disabled entry remains visible in status output but does not connect. |
| `servers.<name>.tool_allow` | `[]` | If non-empty, expose only tools whose original names are listed. |
| `servers.<name>.tool_deny` | `[]` | Never expose these original tool names. Deny wins if a name is in both lists. |
| `servers.<name>.timeout_seconds` | `30.0` | Maximum time for each tool call before an error is returned to the model. |

Restart clipponyai after changing MCP config. Configuration is trusted:
connected tools run with the server's permissions and clipponyai does not show
a per-call approval prompt. Review a server before enabling it and give local
filesystem servers access only to directories you intend to share.

### Local stdio example

This example uses the MCP project's
[filesystem reference server](https://modelcontextprotocol.io/docs/develop/connect-local-servers).
Replace the path with an absolute directory that the server may access. It
requires Node.js and `npx`.

```yaml
mcp:
  enabled: true
  servers:
    filesystem:
      command: npx
      args:
        - -y
        - "@modelcontextprotocol/server-filesystem"
        - /absolute/path/to/allowed/files
      tool_allow:
        - read_file
        - list_directory
      timeout_seconds: 30
```

For Python MCP servers, the same shape works with `command: uvx` and the
package name as the first item in `args`.

### Remote HTTP example

Keep tokens out of YAML. Put the placeholder in config, export the environment
variable in the environment that launches clipponyai, and restart the app.

```yaml
mcp:
  enabled: true
  servers:
    notes-api:
      url: http://example-host:8080/mcp
      headers:
        Authorization: "Bearer ${MY_MCP_TOKEN}"
      tool_allow: [search_notes, read_note]
      tool_deny: [delete_note]
      timeout_seconds: 20
```

```sh
export MY_MCP_TOKEN="replace-with-the-real-token"
clipponyai check-mcp --server notes-api
```

`${ENV_VAR}` placeholders work in `headers` and `env` values. A missing
variable leaves that server in `ERROR` state instead of starting it with a
broken or literal secret.

### Tool discovery and names

On connection, clipponyai discovers the server's tools and exposes allowed
ones to the FAST chat lane. Names are made collision-safe as
`mcp__<server>__<tool>`: for example, `search_notes` from `notes-api` becomes
`mcp__notes-api__search_notes`. The pony chooses tools from their descriptions
and JSON schemas; you normally refer to them in plain language.

Inspect all configured servers:

```sh
clipponyai check-mcp
```

Inspect one entry:

```sh
clipponyai check-mcp --server filesystem
```

The command returns exit code 0 when all selected, enabled servers connect. It
also prints connection state, allowed tool count, server-instructions
presence, discovered names, or the latest error.

### Troubleshooting

| Status or symptom | Likely cause | What to do |
|---|---|---|
| `MCP disabled` | `mcp.enabled` is false, or no servers exist | Set `mcp.enabled: true` and add an entry under `mcp.servers`. |
| `DISABLED` | That server has `enabled: false` | Enable it when ready; disabled servers do not make `check-mcp` fail. |
| `ERROR` mentioning a missing variable | A `${NAME}` placeholder is not exported | Export it in the app's launch environment, then restart. |
| `ERROR` / executable not found | `command` is absent from the launch `PATH` | Install it or use an absolute executable path. For `npx`, install Node.js. |
| Connection refused or timed out | Wrong `url`, service down, firewall, or unavailable network route | Test the URL from the same machine and verify the server listens on the configured host/port. |
| HTTP 401/403 | Missing, malformed, or rejected authorization header | Check the header format and rotate/re-export the token if needed. |
| Connected with zero/few tools | Filters exclude them, or the server exposes no tools | Temporarily inspect without `tool_allow`/`tool_deny`, then add a focused allow-list. |
| Tool call returns `ERROR: ... timed out` | The call exceeded `timeout_seconds` | Fix the slow server or raise the per-server timeout deliberately. |
| Config validation rejects a server | Both or neither of `command` and `url` are set, or the server name is invalid | Set exactly one connection target and use a simple alphanumeric/underscore/hyphen name. |

## Agent Skills

A skill is a directory whose name matches the `name` in a `SKILL.md` file:

```text
my-writing-style/
├── SKILL.md
└── references/
    └── examples.md
```

Minimal `SKILL.md`:

```markdown
---
name: my-writing-style
description: Writes concise project updates. Use when drafting a status summary.
---

# Instructions

Lead with the outcome, then list blockers and next actions.
Read `references/examples.md` when an example is useful.
```

The `name` must use lowercase letters, numbers, and single hyphens, be at most
64 characters, and match its directory. `description` is required and should
say both what the skill does and when it applies. See the
[Agent Skills specification](https://agentskills.io/specification) for the
portable format.

### Install and configure skills

clipponyai scans these directories in order:

1. `<data_dir>/skills` — the platform-specific clipponyai data directory
   (`~/.local/share/clipponyai/skills` on Linux and
   `~/Library/Application Support/clipponyai/skills` on macOS)
2. `~/.agents/skills` — the cross-client convention
3. each directory listed in `skills.dirs`

The first skill found wins when multiple directories contain the same name.
Extra paths support `~` expansion.

```yaml
skills:
  enabled: true
  dirs:
    - ~/my-agent-skills
  disabled:
    - a-skill-to-hide
```

`skills.enabled` defaults to true. `skills.dirs` adds scan locations;
`skills.disabled` hides skills by frontmatter name without deleting them.
Restart the pony after changing these settings or installing a skill.

Skills use progressive disclosure to conserve context. At startup the model
sees only a catalog of names and descriptions. When a task matches, it calls
`activate_skill` to load that skill's `SKILL.md` body. If the instructions
refer to another text file, it can call `read_skill_file` to load that
resource on demand. The bundled
[commit message skill](examples/skills/commit-messages/SKILL.md) is a complete
copyable example.

clipponyai v1 does **not** execute files under a skill's `scripts/` directory.
Skills can provide instructions and bounded text resources, but not shell or
Python execution.

## Guidance for local models

Small local models become less reliable when they must choose among huge tool
sets. Keep the total modest: connect only services you use, prefer
`tool_allow` over exposing every tool, and use `tool_deny` for unsafe or
irrelevant operations. A narrow collection with clear tool descriptions is
usually more accurate than a catalog of dozens of overlapping tools.

## Commented config template

[`examples/personalization.yaml`](examples/personalization.yaml) contains
inert, fully commented MCP and Skills blocks to copy beside the config created
by `clipponyai init`. The template is separate because YAML serialization
cannot preserve comments when the settings dialog later saves the live
config.
