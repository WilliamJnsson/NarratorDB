# NarratorDB for Codex

Private, local-first long-term memory for Codex. The plugin starts NarratorDB as a local stdio MCP server, keeps the database under the user's NarratorDB data directory, and enables selectable preference and session continuity.

## Install

Prerequisites: Codex, Git, and [`uv`](https://docs.astral.sh/uv/) with the `uvx` command available.

```bash
codex plugin marketplace add WilliamJnsson/NarratorDB
codex plugin add narratordb@narratordb-plugins
```

Restart Codex after installation. Then ask:

```text
Use NarratorDB onboarding for this project.
```

Open the intended project folder before starting that Codex session. From the
CLI:

```bash
cd /path/to/your/project
codex
```

The first MCP start resolves the NarratorDB package from the repository and initializes `~/.narratordb/memory.db` in Private mode with Sessions capture. No NarratorDB account or model-provider API key is required. Use the setup skill to choose Private or Intelligence and Manual, Preferences, or Sessions capture.

## What is included

- Local stdio MCP server launched through `uvx`.
- Clean MCP tools: `configure`, `remember`, `remember_session`, `recall`, `resume`, `forget`, and `status`.
- Skills for setup, onboarding, remembering, safe deletion, and read-only health checks.
- Silent `PreCompact` and `Stop` session-capture hooks. There is no
  `UserPromptSubmit` hook. MCP server instructions remain static; stored data
  is returned only by explicit `recall` and `resume` calls.

`remember.source` accepts only `user`, `assistant`, or `memory`; human
descriptions belong in the memory content. Caller-controlled `system`,
`developer`, and `tool` roles are rejected, and source never raises instruction
authority. MCP tools show short human receipts while keeping IDs, scope, counts,
latency, health, and memory trust in structured metadata for clients that need
diagnostics.

The hooks call `narratordb-hook <event>` from the same package source. Each invocation:

- rejects `UserPromptSubmit` and unknown event names before launching the hook
  runtime;
- runs offline without invoking a compiler model (and preserves the mode of an
  existing database);
- receives an allowlisted environment with provider credentials removed;
- is limited to eight seconds, 1 MiB of input, and 64 KiB of output;
- runs offline from the package cache and cannot download models;
- suppresses errors and always lets Codex continue.

Because hook execution is offline, a cold installation may skip its first hook until the MCP server has populated the `uvx` cache. This is expected fail-open behavior; restart Codex or run onboarding after the MCP server starts.

The release plugin pins both its MCP server and hook wrapper to the same full
Git commit:

```text
narratordb-memory[mcp] @ git+https://github.com/WilliamJnsson/NarratorDB.git@5cda4adbc5c72bec06fa5a63a81bae42369007ec
```

The full commit lets the offline hook reuse the checkout cached by the online
MCP start without resolving a symbolic Git tag.

## Scope safety and interaction

Status reports whether project scope came from an explicit workspace ID, a Git
remote, or a local-path fallback, plus any warning. It reports current-project
and current-user-total memory counts separately.

If Codex starts from the home directory outside a Git repository, NarratorDB
blocks project writes and project session capture. Global
recall and typed current-prompt personal preference capture remain available;
the whole home transcript is never promoted globally. Restart Codex from the intended
project folder. Only after confirming that home is the intended scope, set
`NARRATORDB_ALLOW_PATH_FALLBACK_WRITES=true` before launching Codex. A non-home,
non-Git directory remains writable with a warning that its scope is
machine-local; use `NARRATORDB_WORKSPACE_ID` for a portable identity.

Onboarding and health each use one read-only status call and return a concise
line rather than raw JSON. Automatic preference capture emits no hook context.
Codex may briefly show the native status message while a hook is running;
plugins cannot define a custom spinner or animation.

## Data and privacy

Private mode stores memory locally and does not run a compiler model. Intelligence keeps the same local database and recall, while its explicitly configured write-time compiler can process queued Sessions in the background. Hooks never inherit provider credentials. The plugin contains no telemetry, hosted NarratorDB endpoint, embedded credential, or secret prompt.

Do not store credentials in memory. The bundled remember skill instructs Codex to replace secret-bearing details with a safe statement instead.

## Update or remove

```bash
codex plugin marketplace upgrade narratordb-plugins
codex plugin remove narratordb@narratordb-plugins
codex plugin marketplace remove narratordb-plugins
```

Removing the plugin does not delete `~/.narratordb/memory.db`.

## Troubleshooting

- No tools: confirm `uvx --version`, then restart Codex.
- Hook exit 127: upgrade to 2.1.4 or newer; app hooks use absolute launch paths.
- First hook skipped: let the MCP server start once so its full-commit checkout
  is cached, then start a new session.
- Project scope unsafe: restart Codex from the intended project folder; do not
  enable the home fallback without verifying it.
- Degraded status: use the bundled health skill and follow its single recovery action.
- Duplicate tools: remove any manual `narratordb` MCP entry before installing this plugin.
