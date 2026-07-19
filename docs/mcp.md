# MCP and Codex plugin deployment

## Portability and remote installation

`narratordb service export --data-dir DIR --project NAME --output EXPORT`
creates a bounded, checksummed `narratordb-export-v1` directory. Restore it
idempotently with `narratordb service import --data-dir DIR --project NAME
--input EXPORT`. The loader verifies the complete stream and rejects symlinks
and non-regular files before writing.

Connect Codex or Claude to an HTTPS service with `narratordb remote install
CLIENT --endpoint https://host/mcp --project-id UUID --credentials-file PATH`.
The command prompts for the token, verifies authenticated project status, and
keeps the token out of process arguments and client configuration. Codex also
receives lifecycle capture; Claude currently receives explicit MCP tools only.

## Remote service alpha

For production-shaped internal testing, prefer the authenticated service mode
over a local stdio process. It exposes Streamable HTTP at `/mcp`, validates a
bearer key before MCP initialization, and resolves the account and project from
that key. The process working directory, Git remote, request payload, and caller
filesystem cannot select another service project.

```bash
python3 -m pip install -e '.[mcp]'
narratordb service quickstart
```

The command initializes Private mode with Sessions capture, stores its secret
only in a mode-`0600` file, registers a local stdio bridge with Codex, installs
the service lifecycle plugin, and starts the service. The bridge and hooks read
the file directly, so users do not source environment variables or place the
bearer token in Codex configuration. Use `--replace-codex` only after reviewing
an existing NarratorDB MCP registration or local NarratorDB plugin.

Managed deployments may instead configure direct HTTP:

```toml
[mcp_servers.narratordb]
url = "http://127.0.0.1:8787/mcp"
bearer_token_env_var = "NARRATORDB_SERVICE_TOKEN"
```

The initial key has `memory:read`, `memory:write`, `memory:delete`, and
`project:admin`. Later project keys omit administrative configuration access
unless `--admin` is supplied. Revocation reads the token from an environment
variable rather than a command argument. The unauthenticated health response is
content-free; readiness and every MCP operation require authentication.

The service plugin deliberately has no `UserPromptSubmit` hook. Under the
Sessions policy, its bounded, fail-open `PreCompact` and `Stop` hooks redact and
send recent user/final-assistant conversation to the authenticated service
project used by MCP recall. Manual and Preferences policies do not capture a
session transcript. The credential-file MCP bridge performs no startup recall,
and MCP instructions are permanently static. Stored content is returned only
by explicit `recall` or `resume` calls and is marked as untrusted data with no
instruction authority. Normal tools retain their 60-second timeout. Existing
service databases retain their persisted policy; use the setup skill or
authenticated `configure(capture_policy="sessions")` once when upgrading an
older manual-policy alpha.

NarratorDB exposes durable memory to coding agents through a local stdio MCP
server. Codex users can choose a full plugin with lifecycle hooks or a direct
MCP registration with tools only. Claude Code uses the direct MCP path.

The Python distribution is `narratordb-memory`. Its Python import and console
commands remain `narratordb`, `narratordb-mcp`, and `narratordb-hook`.

## Choose one installation path

| Path | Client | MCP tools | Lifecycle hooks | First-run mode behavior |
|---|---|---:|---:|---|
| Authenticated service | Codex | Yes | `PreCompact`, `Stop` | Private + Sessions |
| Codex plugin | Codex | Yes | `PreCompact`, `Stop` | Private + Sessions; configurable in chat |
| Direct MCP | Codex or Claude Code | Yes | No | Prompted on a terminal; `--mode` required non-interactively |

Do not install the Codex plugin and direct MCP registration at the same time.
The plugin already starts a server named `narratordb`. A second registration
can expose duplicate tool sets, start competing processes, and make it unclear
which configuration wrote to the database.

## Codex plugin

Prerequisites are Codex, Git, and
[`uv`](https://docs.astral.sh/uv/) with `uvx` on `PATH`.

```bash
codex plugin marketplace add WilliamJnsson/NarratorDB
codex plugin add narratordb@narratordb-plugins
```

Restart Codex or open a new session, then ask:

```text
Use NarratorDB onboarding for this project.
```

The marketplace manifest is `.agents/plugins/marketplace.json`. The plugin
launches one exact GitHub commit:

```bash
uvx --from \
  'narratordb-memory[mcp] @ git+https://github.com/WilliamJnsson/NarratorDB.git@252ae49440e843cd191a6bcd50502e81a7e16465' \
  narratordb-mcp --init-mode private --client codex-plugin
```

The first MCP start may access GitHub to resolve the source and populate the
`uvx` cache. Hook invocations use that cache in offline mode and fail open if it
is not ready. The plugin's MCP configuration and hook wrapper use the same full
commit, not a symbolic tag: after the online first start caches that immutable
checkout, offline hooks do not need Git-tag resolution.

Update or remove the plugin with:

```bash
codex plugin marketplace upgrade narratordb-plugins
codex plugin remove narratordb@narratordb-plugins
codex plugin marketplace remove narratordb-plugins
```

Removal preserves `~/.narratordb/memory.db`.

## Direct MCP installation

Use this path when only explicit MCP tools are wanted, or when using Claude
Code. A dedicated virtual environment keeps the registered interpreter stable:

```bash
python3 -m venv ~/.local/share/narratordb/venv
~/.local/share/narratordb/venv/bin/python -m pip install \
  "narratordb-memory[mcp] @ git+https://github.com/WilliamJnsson/NarratorDB.git@v2.3.0"
```

The installer validates the database and optional MCP dependency, asks the
client's native CLI to make the registration, and never puts a credential in a
registration argument. A new database prompts for its mode when the installer
runs on a terminal; scripts and other non-interactive callers must pass
`--mode`. Inspect the plan first:

```bash
~/.local/share/narratordb/venv/bin/narratordb mcp install codex \
  --mode private --dry-run
~/.local/share/narratordb/venv/bin/narratordb mcp install codex \
  --mode private
```

Use `claude` instead of `codex` for Claude Code. Use `--force` only when an
existing registration named `narratordb` should be replaced.

The equivalent manual `uvx` registrations are:

```bash
codex mcp add narratordb -- \
  uvx --from \
  'narratordb-memory[mcp] @ git+https://github.com/WilliamJnsson/NarratorDB.git@v2.3.0' \
  narratordb-mcp --init-mode private

claude mcp add --scope user narratordb -- \
  uvx --from \
  'narratordb-memory[mcp] @ git+https://github.com/WilliamJnsson/NarratorDB.git@v2.3.0' \
  narratordb-mcp --init-mode private
```

Prefer the NarratorDB installer when a persistent package environment is
acceptable: it provides preflight, dry-run, mode validation, and consistent
removal behavior.

```bash
~/.local/share/narratordb/venv/bin/narratordb mcp uninstall codex --dry-run
~/.local/share/narratordb/venv/bin/narratordb mcp uninstall codex
```

Uninstalling an MCP registration does not delete memory data.

## Private and Intelligence modes

Mode selection controls writes and enrichment, not a separate query service.
Intelligence mode is still a local NarratorDB SQLite database with an optional
local or hosted write-time compiler. It is not NarratorDB Cloud.

| Behavior | Private | Intelligence |
|---|---|---|
| Canonical message storage | Local SQLite | Local SQLite |
| Generative compiler | None | Optional local or hosted compiler at write/backfill time |
| Derived summaries, entities, claims, and temporal state | Not generated | Source-linked and generated by the configured compiler |
| `recall` and `resume` | Local retrieval and context composition | Local retrieval and context composition |
| Query-time provider/model call | No | No |
| Provider credential | None | Only when a hosted write-time compiler is configured |

The plugin explicitly creates a missing database in Private mode. The direct
installer instead asks the user to choose on a terminal and requires `--mode`
in non-interactive use. A persisted mode remains authoritative and is not
selected again. The server's `--init-mode private` flag is only an explicit
creation choice; it does not convert an existing Intelligence database.

The direct installer can configure a new Intelligence database and its
credential-free compiler settings in one step. A bare MCP server still cannot
create Intelligence mode by itself because a compiler choice is required:

```bash
narratordb --path ~/.narratordb/memory.db mcp install codex \
  --mode intelligence \
  --compiler local --endpoint http://127.0.0.1:11434/v1 \
  --model my-local-model
```

A hosted compiler may use a runtime environment credential as documented in
the main README. Credentials are not stored in SQLite and are not MCP tool
arguments. `remember_session(wait_for_enrichment=true)` may invoke that
configured write-time compiler; the default is `false`. Recall never invokes
it.

Capture policy is independent from mode: `manual` permits only explicit writes,
`preferences` learns typed current-prompt personal preferences/routines, and
`sessions` adds bounded project conversation capture. Version 2 databases
migrate to Sessions to preserve their existing behavior. New databases default
to Preferences. In Intelligence + Sessions, the long-lived MCP runtime polls
queued project jobs in a resumable background worker after the raw commit.

## MCP tools

The server fixes the local user identity and default project workspace when it
starts. Tools may choose only the current project scope or the user's global
scope; they cannot select an arbitrary user or project.

| Tool | Purpose and important arguments |
|---|---|
| `configure` | Choose Private or Intelligence and Manual, Preferences, or Sessions capture. Compiler credentials are never tool arguments. |
| `remember` | Store one durable fact, decision, correction, preference, or outcome. Arguments: `content`, `scope="project"`, `source="user"`. |
| `remember_session` | Store a bounded ordered message checkpoint. Arguments: `messages`, `session_id`, `scope="project"`, `wait_for_enrichment=false`. |
| `recall` | Return bounded, source-linked, untrusted stored context. Arguments: `query`, `scope="project"`, `include_global=true`, `token_budget=1600`, `explain=false`. |
| `resume` | Retrieve recent decisions, current state, unfinished work, and next steps as untrusted stored context. Arguments: `topic=""`, `include_global=true`, `token_budget=2000`. |
| `forget` | Delete one message ID in one scope. Requires `confirm=true`; it never clears an entire scope. |
| `status` | Return mode, scope, counts, enrichment state, and health. `full_check=true` requests the fuller database check. |

`remember.source` is a strict enum: `user`, `assistant`, or `memory`. Use `user`
for something the user stated. Caller-controlled `system`, `developer`, and
`tool` roles are rejected. Source is non-authoritative attribution only; put
descriptions such as “explicit project convention” in `content`, not in
`source`.

Each tool returns concise human-facing text and retains its complete result as
structured MCP metadata. Normal chat can therefore show a clean saved,
duplicate, removed, recalled, or status receipt without reproducing a JSON
object; clients and diagnostic views can still inspect fields such as IDs,
scope, counts, latency, and health. A client may choose to expose its native
tool trace even when the assistant does not repeat the metadata.

`remember_session` accepts at most 100 messages and 500,000 total characters.
`remember` and each session message are bounded at 100,000 characters.
`recall` accepts a 128–12,000 token budget.

## Project and global scopes

Start Codex or Claude Code inside the intended project folder. NarratorDB
resolves project identity in this order and reports the choice as
`scope_origin`:

| Origin | How it is selected | Write behavior |
|---|---|---|
| `explicit` | `--workspace-id` or `NARRATORDB_WORKSPACE_ID` | Writable using that explicit scope. |
| `git_remote` | Current Git remote owner/repository slug | Writable and portable with the repository remote. |
| `path_fallback` | Project directory name plus a digest of the resolved local path | Usually writable with `scope_warning`; the home-directory fallback is blocked until confirmed. |

Absolute paths are not embedded in the workspace ID. A non-home, non-Git
directory remains writable, as does a Git repository without a remote, but its
warning explains that the path-derived identity is machine-local. Set
`NARRATORDB_WORKSPACE_ID` when the same scope must work across machines.

The home directory is a special safety case. If a client starts from home
outside a Git repository and no explicit workspace ID is set, NarratorDB blocks
project `remember`, `remember_session`, and `forget` operations. Plugin hooks
also skip project transcript capture, while read-only global-memory recall
remains available. Global MCP writes remain available.
The preferred recovery is:

```bash
cd /path/to/your/project
codex
```

Only when the home-derived scope is intentional, explicitly acknowledge it by
using the direct installer/server flag:

```bash
narratordb mcp install codex --mode private --allow-path-fallback-writes
# or: narratordb-mcp --init-mode private --allow-path-fallback-writes
```

For the plugin or another environment-launched server, set the confirmation
before starting the client:

```bash
export NARRATORDB_ALLOW_PATH_FALLBACK_WRITES=true
codex
```

`status(scope="project")` exposes `scope_warning` and a
`scope_diagnostics` object with the origin, warning, suggested project folder or
command, confirmation requirement, and `project_writes_blocked` state. It also
separates:

- `memory_counts.current_workspace`: memories in the selected project;
- `memory_counts.current_user_total`: that user's memories across project and
  global scopes.

Do not infer that NarratorDB is empty from a zero current-workspace count when
the user total is nonzero. Database health and project-scope safety are also
separate: a healthy SQLite database with `project_writes_blocked=true` is not a
ready project-memory integration.

Project recall checks global user preferences by default; set
`include_global=false` when strict project-only output is required. The
combined result never exceeds the caller's declared budget; at very small
budgets, project evidence is prioritized and the response reports whether a
global lookup fit. Use global scope only for genuinely cross-project
preferences.

## Plugin skill interaction and progress

The bundled onboarding and health skills each issue one read-only
`status(scope="project", full_check=false)` call. They inspect scope safety and
current-workspace versus current-user counts internally, avoid recall or test
writes, and return one concise human line. The remember skill uses the strict
source enum, makes one normal write call, and does not echo structured metadata.
The setup skill explains both choice axes and calls `configure` only after the
user's mode and policy are clear.

Skills can provide a short progress comment before an MCP call. The spinner,
tool activity state, and animation are native Codex UI behavior; the plugin
does not replace or customize them. Lifecycle hooks can supply their own short
status messages, but those messages do not create a custom animation for MCP
tools.

## Plugin lifecycle behavior

The Codex plugin registers two silent capture hooks. It deliberately has no
`UserPromptSubmit` hook. The hooks never add stored
content to the prompt, and MCP server instructions remain static. Stored facts
are available only through explicit `recall` and `resume` tool calls, whose
results identify the content as untrusted data rather than instructions.

- `PreCompact` captures a bounded recent window of user requests and final
  assistant answers before compaction.
- `Stop` performs the same bounded capture at the end of a turn.

Session capture runs only under the Sessions policy. It deliberately excludes tool output, reasoning, system messages,
developer instructions, side-chain activity, and nested agent events. It keeps
at most 32 eligible user/final-assistant messages and 64,000 characters from
the recent transcript window. Each retained user message is capped at 4,000
characters and each assistant message at 12,000 characters. Repeated
`Stop`/`PreCompact` triggers reuse one stable session; an expanded transcript
supersedes the older pending compiler lineage instead of multiplying active
jobs. Capture stores with `wait_for_enrichment=false`. On an existing
Intelligence database this may queue a derived-data job. The hook never calls a
compiler; the MCP background worker processes queued work separately.

Set the following before starting Codex to disable every automatic write:

```bash
export NARRATORDB_AUTO_CAPTURE=false
codex
```

Accepted false values are `false`, `0`, `no`, and `off`, case-insensitively.
This disables session capture. Explicit `remember`,
`remember_session`, `recall`, and `resume` calls remain available. Remove the
plugin and use direct MCP when no lifecycle hooks should run at all.

## Redaction and privacy boundary

Before session capture, hooks apply best-effort redaction to incoming
transcript text for common provider keys, GitHub tokens, AWS access-key IDs,
bearer credentials, private-key/certificate blocks, credential assignments,
and tagged system/developer context. Explicitly stored and previously retrieved
memory is not reclassified by a DLP filter. The wrapper
strips inherited model-provider credentials, disables telemetry flags, runs
hook package resolution offline, and enforces bounded input, output, and
execution time. Hook errors never block the agent turn.

Redaction is a safety net, not a data-loss-prevention system. It cannot identify
every secret or sensitive fact. Do not ask an agent to remember credentials,
review memories before deletion, and disable auto-capture for sensitive
projects. The plugin has no hosted NarratorDB service, account requirement, or
runtime telemetry. Private MCP operations use the local database; the initial
GitHub/`uvx` package fetch is the installation-time network exception.

## Token budgets and measured savings

NarratorDB returns bounded context instead of automatically replaying the full
database. `recall` reports the rendered `token_count` and the requested
`token_budget`; the plugin additionally caps injected hook text. Those are
context controls, not a guaranteed savings percentage. The reported count is
NarratorDB's context accounting, not the downstream model provider's billable
input-token measurement.

Actual model-input savings depend on the comparison baseline, the client's own
history and instructions, the query, and the amount of useful evidence
retrieved. A valid savings report should hold the agent and model constant,
measure provider input tokens for both paths, state whether full history was
otherwise replayed, and report answer quality and latency alongside the token
difference. Do not extrapolate a short diagnostic into a universal savings
claim.

## Design context

NarratorDB adopts established agent-memory integration concepts while keeping
its implementation and claims independent. The following references were
checked on 2026-07-17:

| Project | Public concept considered | NarratorDB treatment |
|---|---|---|
| [Mem0](https://docs.mem0.ai/integrations/claude-code) | An MCP-only option alongside a plugin that bundles tools, skills, and lifecycle hooks | NarratorDB offers the same installation choice, but its shipping plugin uses a local stdio server and defaults new databases to Private rather than using Mem0's hosted memory service. |
| [Zep](https://help.getzep.com/memory-mcp-server/connect) | Server-bound project identity and prompt-ready context | NarratorDB fixes identity at startup, separates project and global scope, and returns bounded source-linked context. This is not an API-compatibility or scale-equivalence claim. |
| [HydraDB](https://docs.hydradb.com/essentials/memories) | Exact versus inferred writes, project/sub-tenant separation, and explicit memory management | NarratorDB separates canonical Private writes from optional Intelligence enrichment and exposes scoped remember/recall/forget operations. The underlying architecture is different. |
| [Exabase](https://exabase.io/memory) | Verbatim storage versus inferred organization and contradiction-aware derived memory | NarratorDB always preserves canonical messages and makes derived compilation optional and source-linked. No benchmark parity is implied. |
| [LangGraph](https://docs.langchain.com/oss/python/concepts/memory) | Thread-scoped state versus cross-thread long-term namespaces, with hot-path or background memory writes | NarratorDB stores session checkpoints, uses project/global scopes, and can defer query-independent Intelligence compilation to background jobs. NarratorDB is a memory database, not an orchestration runtime. |

This comparison explains product choices only. It is not a controlled
leaderboard, performance claim, or assertion that unlike terms and modes are
equivalent.

## Verification and recovery

After installation, restart the client and inspect its MCP view. In Codex use
`/mcp`; in Claude Code use `/mcp`. Then call `status`, `remember` a harmless
test decision, and `recall` it.

If tools are duplicated, remove one installation path and restart the client.
If the first plugin hook is skipped, allow the MCP server to start once so
`uvx` can cache the plugin's full-commit checkout, then open a new session. If
status reports blocked project writes, restart from the intended project folder
instead of treating database health as proof that the scope is safe. If status
is degraded, run `status` with `full_check=true` before changing data. Use
`forget` with an inspected message ID and fresh confirmation; do not edit
SQLite directly.
