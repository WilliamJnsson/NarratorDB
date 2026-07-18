# NarratorDB

NarratorDB is an independent, local-first long-term memory database for AI
systems. It stores original conversation text and typed records, then retrieves
them through SQLite FTS5, BM25, optional SentenceTransformer embeddings,
provenance filters, and contextual windows. Private mode needs no generative
model. Intelligence mode can use an optional write-time compiler, but recall
itself remains local and makes no query-time model call.

NarratorDB owns its package, interfaces, tests, release version, and data path.
Its default database is `~/.narratordb/memory.db`; it does not inspect or fall
back to another product's files or environment variables.

Community is unlimited and MIT licensed: local SQLite memory, stdio MCP, the
authenticated self-hosted service, Docker, and portable export/import. Managed
multi-tenant storage, billing, and hosted infrastructure are a separate product
and are not shipped in this package. See the [open-core architecture decision](docs/architecture/0001-open-core-cloud-boundary.md).

NarratorDB 2.2 supports Python 3.10 through the current stable Python 3.14.

## Website and product direction

The NarratorDB website lives in [`website/`](website/). It is the proprietary
cloud product site, organized around six explicit product surfaces: the
homepage, product, research, pricing, cloud access, and the functional dashboard
preview. Legacy routes redirect to the relevant canonical section. The current private deployment is available at
[narratordb-home.william615395.chatgpt.site](https://narratordb-home.william615395.chatgpt.site).

The dashboard preview at [`/dashboard`](https://narratordb-home.william615395.chatgpt.site/dashboard)
is an engineering-first control-plane demonstration. Its application shell
includes project/environment context, responsive navigation, global search,
health state, and ten focused views: overview, canonical records, entities and
scopes, retrieval lab, activity, integrations, API keys, team access, usage,
and settings. Filters, record inspection, bulk archive/delete actions,
deterministic retrieval runs, preview key issuance/revocation, integration
state, invitations, role changes, and settings are functional. Changes persist
only for the browser session, are clearly labeled as preview data, and can be
reset without touching live infrastructure.

The research page compares NarratorDB with Mem0, Zep, HydraDB, Hindsight,
Supermemory, Exabase, Mastra, LangMem, and Letta. Every published number keeps
its benchmark, configuration, source, verification date, and comparability
warning attached; the page is research context, not a controlled leaderboard.
NarratorDB's public research link serves a sanitized, site-owned copy of the
frozen 2026-07-15 record while the complete historical record remains in this
repository.
The interface uses a white-and-black editorial frame separated by acid
chartreuse, editorial pink, safety coral, industrial silver, and inverted ink
content fields. Orange chart marks and green ready/available states keep their
distinct roles. The homepage's old stripe treatment has been replaced by a
contained grayscale 3D memory field: projected nodes, depth-aware connections,
soft fog, and restrained pointer parallax occupy the deliberate upper-right
space while preserving the bottom-left typography. Visible interface type has
an enforced 11 px floor, with larger 14–16 px supporting copy, stronger
foreground contrast, and transform-only reveal motion so text remains readable
before client-side animation starts.

The homepage also includes an automatic NarratorDB pipeline simulation. Three
switchable stories—Personal Assistant, fictional Care Coordination, and
Customer Support—continuously demonstrate source ingestion, scoped memory
construction, and provenance-backed retrieval. It pauses while offscreen,
restarts when a visitor changes scenarios, and resolves to a static accessible
state when reduced motion is requested.

The Python package in this repository, including NarratorDB 2.1, is distributed
under the MIT license declared in `pyproject.toml` and `LICENSE`. The commercial
website presents NarratorDB Cloud only; the hosted platform is a separate
proprietary product. Private-deployment, governance, and pricing capabilities
not in private preview are labeled **Planned** or as preview targets; they
should not be read as shipping compliance or SLA claims.
The adaptive cloud-access form stores consented lead records in a Cloudflare D1
database and deliberately omits IP addresses and browser fingerprints.

Run it locally with Node.js 22.13 or newer:

```bash
cd website
npm install
npm run dev
```

The development server prints its local URL, normally
`http://localhost:3000`. Validate the production output with `npm test` and
`npm run lint`.

### Website verification

`npm test` builds the production worker and runs ten suites covering
canonical pages, visible-type and reveal-style safeguards, legacy redirects,
sourced provider research, commercial positioning, D1 lead
validation/persistence, the automatic homepage showcase, the 3D hero field,
the complete dashboard route set, and unknown-route handling. `npm run lint`
validates the application source.
Future visual changes should be reviewed at desktop and mobile widths with both
normal and reduced motion.

## Install and use

### Authenticated service alpha

NarratorDB includes an internal service alpha for testing the same deployment
boundary used by hosted memory products: a long-lived Streamable HTTP MCP
server, bearer authentication, and server-issued project identity. Service mode
never derives authorization from the server's working directory or Git remote.
The local stdio integration below remains available as a separate embedded
deployment.

Install the MCP extra and start everything with one command:

```bash
python3 -m pip install -e '.[mcp]'
narratordb service quickstart
```

`quickstart` initializes Private mode with Sessions capture on first use, creates
a `0600` credential file under `~/.narratordb/service`, registers a
credential-file stdio bridge through the native `codex mcp` command, installs
the service lifecycle plugin, and starts the authenticated HTTP service. The
service plugin has no `UserPromptSubmit` hook: it sends only bounded, redacted
conversation windows at `PreCompact` and `Stop` to the same authenticated
project used by recall. The token is never placed in shell startup files,
process arguments, the plugin, or Codex configuration. Restart Codex or open a
new session after the first run. At MCP initialization, the credential-file
bridge also makes one project-only, bounded `resume` read and supplies the
result through private server instructions. That startup read fails open after
five seconds if the remote service is cold or unavailable; ordinary MCP tool
calls retain their 60-second timeout. No prompt-submit hook is installed. If
another NarratorDB integration is already registered or the local-database
plugin is installed, review it and rerun with `--replace-codex` to replace it
explicitly.

Advanced operators can still initialize and serve explicitly:

```bash
narratordb service init \
  --data-dir .narratordb-service \
  --project narratorDB \
  --credentials-file .narratordb-credentials/narratordb.env \
  --mode private

narratordb service serve \
  --data-dir .narratordb-service \
  --host 127.0.0.1 \
  --port 8787 \
  --public-url http://127.0.0.1:8787
```

Or let Docker initialize and start a clean service automatically:

```bash
docker compose up --build
```

Docker keeps all mutable state under the mounted `.narratordb-service` directory
while the container root filesystem remains read-only. If first-time setup is
interrupted, its partial database artifacts are rolled back so the next Compose
start can retry cleanly without deleting an independently created credentials
file.

Connect host Codex to that container without exporting its token:

```bash
narratordb service install-codex \
  --credentials-file .narratordb-service/credentials.env
```

This also installs `narratordb-service@narratordb-plugins`. For a service data
directory created by an older alpha, set its persisted capture policy to
Sessions once with the NarratorDB setup skill or authenticated `configure`
tool. New Compose data directories already default to Sessions.

The Compose port is bound only to host loopback. A non-loopback public URL must
use HTTPS behind a TLS-terminating reverse proxy. Process health is available at
`/healthz`; `/readyz` requires the bearer token. Ordinary users do not need to
export it—the installed bridge authenticates automatically. Operators doing a
raw HTTP diagnostic can supply a token explicitly:

```bash
curl -fsS http://127.0.0.1:8787/healthz
curl -fsS http://127.0.0.1:8787/readyz \
  -H "Authorization: Bearer $NARRATORDB_SERVICE_TOKEN"
```

For deployments that already provide OAuth or managed environment injection,
Codex can also connect directly to the remote MCP endpoint:

```toml
[mcp_servers.narratordb]
url = "http://127.0.0.1:8787/mcp"
bearer_token_env_var = "NARRATORDB_SERVICE_TOKEN"
```

The credential-file bridge installed by `quickstart` needs no environment
variable. The key fixes the account and project regardless of Codex's current
directory. Create isolated projects or rotate keys without exposing token values
in command arguments or normal CLI output:

```bash
narratordb service add-project \
  --data-dir .narratordb-service \
  --project second-project \
  --credentials-file .narratordb-credentials/second.env

narratordb service issue-key \
  --data-dir .narratordb-service \
  --project narratorDB \
  --credentials-file .narratordb-credentials/replacement.env

NARRATORDB_SERVICE_TOKEN="$NARRATORDB_SERVICE_TOKEN" \
  narratordb service revoke-key \
  --data-dir .narratordb-service
```

Ordinary keys receive read, write, and single-memory delete scopes. The initial
key also receives `project:admin` so it can use `configure`; pass `--admin`
explicitly when issuing another administrative key. Private and every existing
Intelligence compiler are supported at initialization through the same
credential-free compiler flags as `narratordb init`. Hosted provider credentials
remain server environment variables and existing cost controls still apply.

This is an internal alpha: it is single-account, single-process, and intended
for isolation, restart, and stress testing before OAuth, public signup, billing,
or high availability.

### Codex plugin or direct MCP

NarratorDB now ships a local stdio MCP server for Codex and Claude Code, plus a
Codex plugin that preloads bounded private context through MCP instructions and
adds silent lifecycle capture hooks. The Python
distribution is named `narratordb-memory`; the import and command names remain
`narratordb`.

For the complete Codex integration, install the repository marketplace and
plugin:

```bash
codex plugin marketplace add WilliamJnsson/NarratorDB
codex plugin add narratordb@narratordb-plugins
```

Restart Codex, then ask it to use the NarratorDB onboarding skill. The plugin
starts the GitHub distribution through `uvx`, initializes
`~/.narratordb/memory.db` in Private mode when needed, and exposes `configure`,
`remember`, `remember_session`, `recall`, `resume`, `forget`, and `status`.

For MCP tools without lifecycle hooks, install the same distribution from
GitHub into a dedicated environment and let NarratorDB use the client's native
registration command:

```bash
python3 -m venv ~/.local/share/narratordb/venv
~/.local/share/narratordb/venv/bin/python -m pip install \
  "narratordb-memory[mcp] @ git+https://github.com/WilliamJnsson/NarratorDB.git@v2.2.1"

# Inspect first, then install for exactly one client.
~/.local/share/narratordb/venv/bin/narratordb mcp install codex \
  --mode private --dry-run
~/.local/share/narratordb/venv/bin/narratordb mcp install codex \
  --mode private
```

Replace `codex` with `claude` for Claude Code. The versioned Git tag keeps the
deployment reproducible; use a newer release tag only after reviewing its notes.

> **Choose one Codex installation path.** Do not install both the Codex plugin
> and the direct `narratordb` MCP registration. The plugin already contains the
> MCP server; using both can expose duplicate tools and start competing server
> processes against the same database. Remove the direct entry with
> `~/.local/share/narratordb/venv/bin/narratordb mcp uninstall codex` before
> installing the plugin.

Start Codex from the intended project folder so NarratorDB can select the
correct workspace:

```bash
cd /path/to/your/project
codex
```

MCP status reports `scope_origin` (`explicit`, `git_remote`, or
`path_fallback`), an actionable scope warning when needed, and separate
`memory_counts.current_workspace` and `memory_counts.current_user_total`
values. If Codex starts from the home directory outside a Git repository,
NarratorDB blocks project writes, project-memory injection, and session capture
rather than silently using home as a project. Global recall remains available;
Preferences/Sessions may save only a typed personal preference from the current
prompt and never promote the whole home transcript. Restart from the intended
project folder. Only after
verifying that the fallback is intentional, enable it with the direct-install
`--allow-path-fallback-writes` option or set
`NARRATORDB_ALLOW_PATH_FALLBACK_WRITES=true` before starting Codex. A non-home,
non-Git project remains writable but receives a machine-local identity warning;
set `NARRATORDB_WORKSPACE_ID` when that scope must be portable.

The `remember` tool accepts only `user`, `assistant`, `system`, or `memory` as
its `source`; attribution prose belongs in `content`. MCP clients receive a
short human-readable receipt plus structured metadata, so normal chat avoids a
raw JSON dump while diagnostics remain available. The onboarding and health
skills each make one read-only `status(scope="project", full_check=false)`
call. Their brief progress commentary accompanies Codex's native tool state;
plugins do not define a custom spinner or animation.

Private mode stores and retrieves canonical text locally without a compiler
model. Intelligence mode adds an optional write-time compiler for source-linked
summaries, entities, claims, and temporal state. In both modes, `recall` and
`resume` search and compose context locally; they do not make a query-time
model call. A new Intelligence database must be configured explicitly with
`narratordb init --mode intelligence --compiler ...` before MCP registration.
With Intelligence plus Sessions, the MCP runtime processes queued write-time
enrichment in a resumable background worker after the local commit.

Automatic capture is a persisted choice independent of memory mode:

- `manual`: explicit writes only;
- `preferences` (new-database default): narrow deterministic personal
  preferences and routines from the current prompt;
- `sessions`: Preferences plus bounded user/final-assistant project capture.

Configure it with the plugin setup skill or the CLI:

```bash
narratordb --path ~/.narratordb/memory.db capture-policy sessions
```

Version 2 databases migrate to Sessions to preserve their earlier lifecycle
behavior. `NARRATORDB_AUTO_CAPTURE=false` disables every automatic write while
leaving explicit MCP writes and local recall active.

The MCP response reports its rendered `token_count` and enforces a caller-set
`token_budget`. This bounds injected memory context, but it does not guarantee
token savings: savings must be measured against a declared full-history or
other baseline, and the client may add unrelated prompt context of its own.

See [MCP and Codex plugin deployment](docs/mcp.md) for manual `uvx` commands,
scope behavior, hook privacy controls, Intelligence setup, and removal.

### Python API

From a source checkout, install all optional features for development:

```bash
python3 -m pip install -e '.[all]'
```

```python
from narratordb import NarratorDB

with NarratorDB(user_id="william", mode="private") as memory:
    memory.remember("The release candidate passed the durability suite.")
    result = memory.recall("What passed the durability suite?")
    print(result.text)
    print(memory.health_check(full=True))
```

Every new database requires an explicit mode choice. `private` is a hard
zero-egress mode: raw messages and retrieval stay local, and no generative
model is configured. Existing NarratorDB 1.x databases migrate to `private`
when first opened by 2.0. To configure a project interactively or from CI:

```bash
narratordb --path ./memory.db init --mode private
narratordb --path ./memory.db status
```

Intelligence mode adds a write-time compiler while keeping recall local and
deterministic. The user chooses either a loopback HTTP local model or a
pinned hosted model. Credentials are never stored in the database or accepted
as command-line arguments:

```bash
# Zero-egress intelligence with a local OpenAI-compatible endpoint.
narratordb --path ./memory.db init --mode intelligence \
  --compiler local --endpoint http://127.0.0.1:11434/v1 --model my-local-model

# Hosted experiment; OPENROUTER_API_KEY is read only by the runtime adapter.
OPENROUTER_API_KEY=... narratordb --path ./experiment.db init \
  --mode intelligence --compiler openrouter \
  --model openai/gpt-5.4-mini --provider Azure

# First-party OpenAI API; the endpoint is fixed and OPENAI_API_KEY is runtime-only.
OPENAI_API_KEY=... narratordb --path ./official-openai.db init \
  --mode intelligence --compiler openai \
  --model gpt-5.6-luna --reasoning low

# ChatGPT-subscription experiment through an isolated Codex CLI process.
codex login status
narratordb --path ./subscription-experiment.db init \
  --mode intelligence --compiler codex-cli \
  --model gpt-5.4-mini --reasoning low \
  --codex-cli-version 'codex-cli 0.144.4' \
  --codex-max-concurrency 1
```

The Codex CLI route is separately identified and metered as subscription usage;
it is not presented as the OpenAI API route. Start with the three-session gate
and promotion sequence in
[`docs/codex-cli-compiler.md`](docs/codex-cli-compiler.md), not a full benchmark
ingestion.

The first-party OpenAI route accepts no custom endpoint, provider allowlist, or
OpenRouter fallback. It reads only `OPENAI_API_KEY`, verifies the returned GPT
model identity, and keeps content-free usage/cost records separate from memory
content. Account-level retention settings remain controlled by the OpenAI
project rather than being claimed by NarratorDB.

GPT-5.4 Mini is the economical default for both hosted adapters. The first-party
OpenAI route can explicitly select the current
[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
model as `gpt-5.6-luna`. The separately named OpenRouter experiment remains
`openai/gpt-5.6-luna-pro`; that historical router alias must not be presented as
the first-party model or route. OpenRouter's pinned profiles use Azure with
`minimal` reasoning for GPT-5.4 Mini and `low` reasoning for its Luna Pro alias.

Compilation is session-based: canonical messages commit first, then a
query-independent job produces source-linked summaries, entities, atomic
claims, temporal fields, and stable memory keys. Compiler V7 can also receive
up to eight deterministically selected active prior claims as untrusted hints:
local lexical selection plus a recent-keyed fallback helps an explicit update
reuse the right memory key. Only the current session's canonical messages can
ground or be cited by new output; references cannot be cited, copied forward
without current support, or used as relation targets. V7 also preserves salient
assistant recommendations, resources, plans, and commitments when future
continuity would otherwise be lost.

`wait_for_enrichment=True` processes the job synchronously; omitting it leaves
a resumable job for `narratordb backfill` or an application worker. Engine and
facade recall never call the model. The benchmark compatibility adapter can
lazy-finalize pending sessions before its first local search; clients that use
its explicit query-free finalize endpoint keep that compiler time outside
search.

```python
from narratordb import CompilerConfig, NarratorDB

with NarratorDB(
    db_path="./experiment.db",
    user_id="william",
    mode="intelligence",
    compiler=CompilerConfig.openrouter(),
) as memory:
    memory.ingest_session(
        [
            {"role": "user", "content": "I moved from Oslo to Tokyo."},
            {"role": "assistant", "content": "I'll remember Tokyo as your current city."},
        ],
        session_id="conversation-2026-07-15",
        wait_for_enrichment=True,
    )
    context = memory.recall_context("Where do I currently live?", token_budget=1200)
    print(context.text)
```

Equivalent current-session content plus the exact V7 reference-claim context is
compiled once through a persistent, validated SQLite cache. Cache hits rebind
evidence to the current canonical message IDs and add no model usage or cost.
`purge --yes` securely clears both derived records and cached compiled output
while retaining raw messages.

Intelligence context construction uses weighted rank fusion across raw hybrid
hits and claim search, reinforces claim/source support, deduplicates canonical
messages, and adds a bounded query-anchored neighborhood around relevant
session evidence. Structural excerpts preserve requested numbered items and
URL-bearing lines. The requested candidate count is independent of the
rendered context token budget, and filtered searches disable sibling expansion
so it cannot bypass a filter. The lower-level `Engine.search()` API continues
to expose ranked raw-message results directly.

Questions asking for totals, counts, or cumulative history can also receive a
bounded aggregation evidence pack. NarratorDB selects source-linked user events
and facts across distinct sessions, keeps related quantities together, orders
the evidence chronologically, and labels likely retellings. It does not compute
the answer, infer missing events, or call a model at query time; the application
or answer model reasons over the disclosed evidence. Non-aggregation and
current-state questions retain the normal ranking path. Any supplied filter is
applied to raw results, compiled claims, and all pack sources.

Hosted compilation separates semantic repair attempts from transport retries.
Every wire attempt passes through one request-start pacer, bounded provider
retry metadata becomes a durable job cooldown, and terminal or deferred state
survives restarts. Ordered OpenRouter allowlists, exact model checks, ZDR/data
policy requirements, and the retry topology participate in the compiler
fingerprint. Usage and failure ledgers contain only closed route identifiers,
numeric usage/cost, bounded retry metadata, and stable error codes—never prompts
or model output. HTTP protocol failures are retryable ledger events, and a
successful response whose model or provider cannot be attested fails closed
before its content reaches a benchmark scorer. When upstream billing is
missing, the live request reservation becomes a conservative local charge and
the run remains publication-incomplete until provider billing is reconciled.
An operator-set local spend fuse is process-scoped and soft for an in-flight
request; use a provider/account cap for an aggregate hard ceiling.

Mode changes are explicit. Leaving intelligence mode requires choosing whether
to retain or purge derived records; canonical raw messages are never removed
by `purge`.

```bash
narratordb --path ./memory.db backfill
narratordb --path ./memory.db mode private --retain-derived
narratordb --path ./memory.db purge --yes
```

The lower-level API exposes storage and ranked results directly:

```python
from narratordb import Engine

with Engine("/tmp/narratordb.db", user_id="william") as engine:
    engine.store("user", "The launch code is cobalt seven.")
    result = engine.search("launch code", limit=10)
    print([message.text for message in result.messages])
    print(engine.backup("/tmp/narratordb.backup.db"))
```

## Configuration

| Variable | Meaning |
|---|---|
| `NARRATORDB_PATH` | Explicit database file |
| `NARRATORDB_DB_PATH` | Equivalent explicit database file |
| `NARRATORDB_DATA_DIR` | Data directory; defaults to `~/.narratordb` |
| `NARRATORDB_USER_ID` | Default logical user/scope |
| `NARRATORDB_WORKSPACE_ID` | Explicit project scope for agent integrations; otherwise derived from the Git remote or local project path |
| `NARRATORDB_AUTO_CAPTURE` | Set to `false`, `0`, `no`, or `off` before launching Codex to disable every automatic hook write; explicit MCP writes and local recall remain available |
| `NARRATORDB_ALLOW_PATH_FALLBACK_WRITES` | Explicitly allow project writes and hooks when startup resolved to the home-directory fallback; prefer restarting from the intended project folder |
| `OPENAI_API_KEY` | Runtime-only credential for the fixed first-party OpenAI compiler route; never persisted |
| `OPENROUTER_API_KEY` | Runtime-only hosted compiler credential; never persisted |
| `NARRATORDB_COMPILER_MAX_COST_USD` | Optional per-process cumulative soft USD compiler stop; unset by default, and an in-flight call can finish |
| `NARRATORDB_COMPILER_REQUEST_RESERVATION_USD` | Per-request amount reserved against an enabled local compiler stop; defaults to `0.05` USD |
| `NARRATORDB_COMPILER_BUDGET_SAFETY_RESERVE_USD` | Headroom retained below an enabled local compiler stop; defaults to `1.0` USD |
| `NARRATORDB_EMBEDDING_MODEL` | SentenceTransformer model identifier |
| `NARRATORDB_EMBEDDING_MODEL_DIR` | Explicit local embedding model directory |
| `NARRATORDB_LOCAL_ONLY` | Require locally cached embedding assets when true |

## Durability model

- SQLite WAL journaling supports concurrent readers and writers.
- A 30-second busy timeout prevents short write contention from becoming data
  loss, and every public mutation commits before it returns.
- Scope-specific FTS5 indexes avoid cross-user scans.
- Schema and index-format versions are persisted in database metadata. Schema
  v2 stores normalized terms once, migrates older scopes in place, and repairs
  an interrupted per-user backfill at the next startup.
- Cleanup removes corresponding FTS rows, embeddings, provenance, and
  relations so retention cannot create hidden orphan data.
- `health_check()` validates SQLite, WAL mode, metadata versions, record/index
  counts, and orphan records. `full=True` runs SQLite's full integrity check.
- `backup()` uses SQLite's online backup API and verifies the resulting file
  with a full integrity check.

## Interfaces

- `narratordb.Engine`: direct storage, retrieval, typed records, and maintenance.
- `narratordb.NarratorDB`: stable synchronous application interface.
- `python3 -m narratordb.stdio`: generic JSONL request/response interface.
- `narratordb-mcp` / `python3 -m narratordb.mcp_server`: local stdio MCP
  server with `remember`, `remember_session`, `recall`, `resume`, `forget`, and
  `status`; visible output is concise human text while structured metadata
  remains available to MCP clients.
- `narratordb mcp install {codex,claude}`: native-client MCP registration with
  preflight, dry-run, and explicit replacement controls.
- `narratordb-hook`: fail-open, silent `UserPromptSubmit`, `PreCompact`, and
  `Stop` lifecycle capture used by the Codex plugin. Bounded startup recall is
  supplied through MCP server instructions instead of visible hook output.
- `python3 -m narratordb.benchmark_server`: isolated compatibility endpoint for
  official third-party benchmark harnesses.
- `python3 -m narratordb.benchmarks.history`: append-only benchmark archive and
  checksum/secret verification.
- `narratordb-benchmark-reproduction`: create, recheck, seal, and verify
  secret-safe benchmark reproduction manifests with `preflight`,
  `verify-preflight`, `seal`, and `verify-seal`.
- `narratordb-benchmark-budget-audit`: aggregate declared immutable costs and
  live content-free usage ledgers, reject duplicate or sensitive inputs, and
  enforce an explicit USD provider cap without pretending to convert a
  separately declared EUR governance ceiling.
- `python3 -m narratordb.benchmarks.evaluation_audit`: completeness, frozen
  retrieval-payload, answer/judge, score, provider, token, and cost audit for
  official evaluate-only runs, including predeclared development/holdout
  question scopes.
- `python3 -m narratordb.benchmarks.replay`: content-hash and evidence-session
  audit of an archived LongMemEval retrieval run without model calls.
- `python3 -m narratordb.benchmarks.prediction_audit`: aggregate completeness,
  failed-pair, evidence-session, result-count, and stage-latency audit of an
  official predict-only directory.
- `python3 -m narratordb.benchmarks.openrouter_proxy`: content-free usage ledger,
  provider pinning, and soft cost cap for official-harness OpenRouter runs.

## Tests

Run the complete deterministic regression and stress suite:

```bash
./scripts/test.sh
```

Install the test tooling first with `python3 -m pip install -e '.[dev,all]'`.

The suite validates packaging, bytecode compilation, CRUD, multi-scope
isolation, persistence across restart, online backup and restore, concurrent
writers, full integrity checks, the benchmark HTTP contract, provenance,
typed artifacts, code chunks, relations, cleanup, JSONL integration, high-noise
retrieval, 5,000-message scale, and the 1,540-question Mem0 categories 1-4
LoCoMo answer-in-context diagnostic.

Third-party benchmark publications follow the
[reproducibility policy](BENCHMARKS.md#third-party-reproducibility-policy): a
clean empty database and caches, frozen source/dataset/split/harness/dependency
hashes, fully disclosed commands and model settings, raw prediction/evaluation
artifacts with manifests, and cost/latency accounting are mandatory. Published
archive hashes support bit-for-bit artifact verification; fresh hosted-model
results must be evaluated statistically and are not expected to return
byte-identical completions. No hidden cache or pre-saved answer is permitted.

The primary original-author comparable QA scope is documented separately from
vendor forks and short diagnostics. It contains 1,986 LoCoMo questions, 500
LongMemEval-S questions, and all 2,000 BEAM questions across the 128K, 500K,
1M, and 10M tiers. A fresh Intelligence-mode run with first-party GPT-5.6 Luna
is currently estimated at about `$1,059` in API spend. A legacy `$801-$1,504`
planning band is retained for traceability, but its scenario assumptions were
not preserved and it is not an auditable bound. The protocols do not require
NarratorDB's optional paid compiler, so the
corresponding Private-mode API estimate is about `$160`, excluding local
compute. See the
[original-author comparable suite and dated cost model](BENCHMARKS.md#original-author-comparable-qa-suite-and-api-cost-model-2026-07-17)
and its
[`machine-readable estimate`](benchmark_records/original_author_suite_cost_estimate_20260717.json)
before funding or comparing a run. LongMemEval-M is a separate official scale
track and is not silently included in the primary S-track total. These are engineering
estimates, not a completed run, invoice, or spend authorization.

The completed official-OpenAI short diagnostics remain explicitly non-headline:
LoCoMo balanced-40 scored 38/40 (95.0%), while BEAM 100K conversation-0
scored 0.65458 average rubric compliance (65.458%). Their sanitized, checksum-
bound record is
[`benchmark_records/narratordb_gpt5_official_short_diagnostics_20260717.json`](benchmark_records/narratordb_gpt5_official_short_diagnostics_20260717.json).

The frozen 2026-07-15 NarratorDB 1.3.0 build (git bf4a6b1) passed all
deterministic tests with the LoCoMo answer-in-context diagnostic unchanged at
1,301/1,540 and the golden-ordering parity suite intact. Its frozen official
all-500 LongMemEval run completed 500/500 questions and 124,345 conversation
pairs with zero ingestion failures, then the unchanged Mem0 harness evaluated
every frozen result with GLM 5.2 answering and dated DeepSeek V4 Flash judging
at the pre-declared cutoffs of 20 and 50. Scores were 408/500 (81.6%) at top
20 and 414/500 (82.8%) at top 50 — up from 78.0%/76.0% on the 1.2 run — at a
total evaluation cost of $9.98 including the 30-question pre-flight. Every
failed sample remains in the denominator.

The final top-50 accuracy does not beat Mem0's separately published 94.8%
GPT-5 result, and the different answerer/judge prevents an accuracy
head-to-head. Multi-session questions remain the dominant gap (65.4% at top
50 versus 91-97% for knowledge-update and assistant/user single-session
questions; preference was 83.3%). Exact-message evidence-session recall-any
was 489/500 (97.8%) at top 50, but 11 questions still missed labeled evidence
and reader/context reasoning created additional losses. The evidence supports
both retrieval and synthesis work, not a search-is-solved conclusion. See
`benchmark_records/narratordb_full500_glm52_20260715.json` for the complete
frozen record.

### Frozen Intelligence development result

The first complete NarratorDB Intelligence run used compiler prompt V6 on the
restored, inspected 42-question development split. Under the same GLM 5.2
answerer and dated DeepSeek judge family as the 1.3 run, it scored 29/42
(69.0476%) at top 20 and 31/42 (73.8095%) at top 50. The complete
fresh-database/compiler-cache execution plus answer/judge run cost $24.900646
and retained all 42 questions in the denominator with no selective reruns. The
engine worktree was dirty, so the frozen record captures its commit, tracked
diff hash, and source manifest rather than describing the source tree as clean.

A post-hoc same-ID slice of the immutable 1.3 outputs scored 37/42 (88.0952%)
at top 20 and 36/42 (85.7143%) at top 50. That makes the V6 Intelligence result
five questions, or 11.9048 percentage points, lower at top 50. This is a
diagnostic on inspected development data, not a predeclared holdout or a Mem0
comparison.

The later fresh V7 prediction phase was frozen before scoring and reached
36/42 (85.7143%) at top 20 and 38/42 (90.4762%) at top 50. A contemporaneous
re-evaluation of the same frozen V7 payloads reached 35/42 and 38/42, showing
that hosted reader/judge outputs vary even with identical context and
temperature zero. The consumed-development V11 local replay then reached
35/42 at top 20 and 37/42 (88.0952%) at top 50 and was not promoted. Its exact
flip audit found 33/42 byte-identical retrieval payloads; aggregation produced
one cross-session count gain and one cumulative-state loss, while the net lost
point came from a separate identical-context reader/judge flip. These are
development diagnostics, not untouched, third-party, or competitor scores.
V12 addresses the measured aggregation edge generically: evolving cumulative
snapshots retain the ordinary ranking path, while distinct-event packs use
query-relevant structured claims before dense fallback and exclude unrelated
co-located facts. This is an implementation under local validation, not a new
accuracy score.

See
[`the frozen record`](benchmark_records/narratordb_intelligence_dev42_gpt54mini_20260716.json)
and the separate
[`V6 failure postmortem`](benchmark_records/narratordb_intelligence_dev42_gpt54mini_20260716_postmortem.md),
[`V7/V11 paired record`](benchmark_records/narratordb_intelligence_dev42_v7_v11_paired_20260716.json),
and
[`V7/V11 postmortem`](benchmark_records/narratordb_intelligence_dev42_v7_v11_paired_20260716_postmortem.md).

### V18 direct-official OpenAI paired development diagnostic

Here V18 names an evaluation-protocol iteration, not a NarratorDB package
release. R3 is the first complete two-arm result in the V18 R1-R3 attempt
series. The protocol was sealed before its own provider calls, used identical
frozen prediction bytes in both arms, and ran evaluator replication
unconditionally after a score-blind primary transport audit. Replication
rescored the same frozen payloads; it did not rebuild the database, recompile
memory, or rerun retrieval.

| Cutoff | Primary | Replication | Verdict agreement |
|---|---:|---:|---:|
| Top 20 | 39/42 (92.8571%) | 38/42 (90.4762%) | 39/42 (92.8571%) |
| Top 50 | **42/42 (100%)** | **41/42 (97.6190%)** | 41/42 (97.6190%) |

Both top-50 arms exceeded the frozen 40/42 threshold, so the paired lower
top-50 score is **97.6190%**. The direct official OpenAI route pinned
`gpt-5.4-mini-2026-03-17` with high reasoning as both answerer and judge. Each
arm completed 168/168 accepted calls, for 336/336 total, with zero discarded
transients, terminal rejections, hidden retries, or unknown-cost events. The
canary plus pair cost $1.120186500 on the local conservative token ledger;
provider billing was not independently reconciled.

This is a score-exposed, post-hoc consumed-development diagnostic, and the
same model answered and judged. It is not blind, not an untouched holdout, not
an independent-judge score, not a headline benchmark, and not a Mem0
head-to-head. The sanitized record, exact precommit, result hashes, R1/R2
terminal history, and cost disclosure are in the
[`V18 R3 benchmark record`](benchmark_records/narratordb_intelligence_dev42_v18_gpt54mini_official_r3_20260717.json).

## Speed

The 1.3 cycle profiled the complete official search path on the rebuilt
full-500 corpus (245,780 messages, 500 isolated scopes, 1.8 GB) before
changing code, which decomposed the published 769.70 ms mean into three
distinct layers with three distinct fixes:

1. **Cold SQLite page I/O (~630 ms of the 1.2 mean).** The official run
   searches each scope exactly once, so every query paid first-touch page
   reads against the large database file through a private connection cache.
   A 2 GB `mmap_size` window now lets all per-scope connections share the OS
   page cache.
2. **Pure-Python rerank cost (87% of warm single-request time).** The three
   per-row scoring loops compiled ~6 regexes on every call and lowered,
   tokenized, and pattern-matched each candidate row up to three times. The
   fused single-pass reranker does each exactly once and runs evidence
   patterns only when the query intent consumes them: uncontended
   official-shape search fell from 40.31 ms to 12.94 ms (rerank stage
   27.88 ms to 7.91 ms).
3. **Result-set width.** Retrieval-time shaping trims the median result list
   from 37 to 28 with evidence coverage at the declared cutoffs pinned (by
   full-500 ablation) exactly to the unshaped baseline.

Measured outcomes on the frozen 1.3.0 run, unchanged official harness:

| Metric | 1.2 | 1.3.0 | Mem0 Platform ref |
|---|---:|---:|---:|
| Official HTTP search, mean | 769.70 ms | **30.57 ms** | 2,381.88 ms |
| Official HTTP search, median | 790.45 ms | **19.05 ms** | 2,468.1 ms |
| Official HTTP search, p95 | 1,458.8 ms | **86.1 ms** | 3,353.5 ms |
| Uncontended engine search (limit 200) | 40.31 ms | **12.94 ms** | — |
| LoCoMo internal query mean | 6.36 ms | **2.83 ms** | — |

Local versus managed-service infrastructure prevents a controlled latency
claim against Mem0, but the 1.2-to-1.3 comparison is same-machine,
same-harness, same-corpus. Two robustness fixes shipped with this work:
SentenceTransformer encode calls serialize on a process-wide lock (concurrent
encodes on Apple MPS segfault the process), and sustained 10-worker
saturation remains GIL-bound (~508 ms mean warm) — irrelevant to the official
interleaved workload, addressable later with process-based serving.

## NarratorDB 2.0 intelligence track

NarratorDB 2.0 turns the planned cross-session work into an explicit product
choice instead of changing the behavior of existing local databases:

1. **Private mode.** Canonical storage and retrieval remain local, with no
   generative/compiler model in the memory path. A locally cached optional
   SentenceTransformer may contribute semantic retrieval without egress.
2. **Intelligence mode.** A user-selected local or hosted write-time compiler
   derives source-linked summaries, entities, atomic claims, stable memory
   keys, and temporal update chains. Recall stays bounded and local.
3. **Safe lifecycle.** Raw sessions commit before enrichment; jobs are
   resumable and single-claimed; compiled results are persistently cached;
   deletes invalidate content-bearing cache entries; mode changes and purges
   require explicit derived-data policies.
4. **Measured hosted defaults.** The GPT-5.4 Mini default uses Azure-only,
   no-fallback, zero-data-retention routing with minimal reasoning and an
   8,192-token per-call completion bound. Credentials are runtime-only; a
   production spend quota is operator-configured rather than inherited from a
   benchmark. The local ledger stop is cumulative in USD and soft because an
   in-flight call can finish. Paid benchmark runners require an explicit,
   disclosed local stop, while an external account-wide cap remains the
   aggregate backstop. First-party GPT-5.6 Luna and the distinct historical
   OpenRouter Luna Pro alias remain explicit experiment choices.
5. **Benchmark discipline.** The development and holdout question IDs are
   restored and hash-pinned, prediction/evaluation audits are mandatory, and
   historical 1.3 numbers remain the published baseline until a complete 2.0
   run is frozen regardless of outcome.

A controlled Mem0 head-to-head still requires Mem0 Platform access or a
pinned Mem0 OSS configuration so both databases can be evaluated at the same
time with the same model, prompts, provider, cutoffs, retry policy, and
denominator. Until that exists, stronger-model NarratorDB scores and Mem0's
published GPT-5 result must remain explicitly non-identical comparisons.

External datasets and LLM answer/judge runs are intentionally separate from
the deterministic suite. See [BENCHMARKS.md](BENCHMARKS.md) before comparing a
number with another memory vendor.
