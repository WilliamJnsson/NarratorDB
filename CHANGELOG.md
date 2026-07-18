# Changelog

## 2.2.0 - 2026-07-18

- Established the Community/Cloud package boundary: the MIT wheel contains no
  hosted runtime, infrastructure, billing code, or cloud dependencies.
- Added the public `narratordb.mcp_contract` runtime extension interface,
  checksummed `narratordb-export-v1` service project portability, and a secure
  token-prompted remote installer for Codex and Claude Code.

- Added an internal authenticated service alpha with explicit initialization,
  server-issued project identities, hashed and revocable scoped API keys,
  Streamable HTTP MCP, content-free health and authenticated readiness routes,
  Codex bearer-token setup, and a loopback-only Docker Compose deployment.
  Service authorization never derives identity from the process working
  directory or Git metadata; the existing local stdio integration is unchanged.
- Added one-command `service quickstart`, first-boot Docker initialization, and
  a credential-file stdio bridge that registers through `codex mcp` without
  exporting the bearer token or writing it into Codex configuration.
- Added a separate authenticated-service Codex plugin with bounded `PreCompact`
  and `Stop` session capture and no `UserPromptSubmit` hook. Service installation
  now installs that plugin, rejects the conflicting local-database plugin unless
  replacement is explicit, and keeps hooks and recall on the same project.
  New service quickstarts default to Private mode with Sessions capture.
- Hardened container first boot for read-only root filesystems by keeping every
  database open rooted in the mounted service data directory. Failed initial
  setup now rolls back only its own database artifacts, preserves credentials
  created concurrently, and can be retried cleanly.

## 2.1.7 - 2026-07-18

- Made deterministic preference capture understand a single natural
  conversational prefix such as “and”, “also”, or “by the way” before a
  first-person claim. Statements such as “and I like Mercedes” now persist
  without weakening the existing question, command, secret, sensitive-data,
  quote, paste, and non-personal safety rejections.

## 2.1.6 - 2026-07-17

- Made preloaded-memory answers fully natural: the assistant is instructed not
  to announce a memory check before using facts already in startup context.

## 2.1.5 - 2026-07-17

- Clarified MCP startup instructions to use already-preloaded private memory
  directly and reserve visible `recall` calls for facts outside that context.

## 2.1.4 - 2026-07-17

- Moved bounded startup recall into private MCP server instructions and made
  prompt-time preference capture silent. Ordinary first-answer memory no longer
  prints a `UserPromptSubmit hook (completed)` context block; explicit recall
  remains available when a fact is outside the bounded startup summary.
- Fixed Codex app lifecycle hooks failing with exit code 127 when the GUI PATH
  could not resolve `sh`. Hook launchers now use `/bin/sh`, and the wrapper
  resolves `python3` and `uvx` from both PATH and standard macOS locations.
- Added a GUI-style minimal-environment contract test so CLI-only PATH behavior
  cannot mask this integration failure again.

## 2.1.3 - 2026-07-17

- Added persisted `manual`, `preferences`, and `sessions` capture policies.
  Preferences classifies only the current prompt and saves self-contained
  personal preferences, favorites, routines, and response preferences. Unsafe
  questions, hypotheticals, commands, code/pastes, secret-bearing or sensitive
  text, transient wording, and project/deictic text fail closed.
- Added stable automatic-memory keys and an internal ledger. Repeated natural
  preferences deduplicate across sessions; a new value for the same key
  replaces only the prior automatic memory, never an explicit memory.
- Added the MCP `configure` tool and setup skill for choosing Private or
  Intelligence mode plus capture policy without passing credentials. Status and
  onboarding now display the policy. Version 2 databases retain their bounded
  Sessions behavior during the v3 configuration migration.
- Added a resumable Intelligence background worker to the MCP runtime. Session
  evidence commits locally before queued write-time enrichment runs, while
  Private mode starts no compiler or worker.
- Refined native Codex lifecycle status text for recall, learning, compaction,
  and save operations. Codex still owns the spinner and completed-hook row; the
  plugin adds no artificial delay or simulated animation.

## 2.1.2 - 2026-07-17

- Made first-run mode ownership explicit. Lifecycle hooks no longer create or
  configure a missing database, a bare MCP server requires an explicit
  creation mode, and direct MCP installation prompts for Private or
  Intelligence mode on a terminal while requiring `--mode` in automation.
  Existing mode and compiler configuration remain authoritative. A new
  Intelligence database can be configured with a local or hosted write-time
  compiler during the same direct installation; storage and recall remain
  local, and this mode is not the managed NarratorDB Cloud product.
- Cleaned automatic Codex recall presentation. Hook context now begins with a
  compact recalled-memory count and plain project/personal facts. Internal XML
  envelopes, query boilerplate, message IDs, claim IDs, session IDs, and
  provenance citations no longer leak into ordinary answers. Provenance remains
  stored and available for explicit diagnostics. Codex's native animated status
  messages remain the loading UI.
- Preserved Private and Intelligence behavior across sessions, including the
  strict remember schema, concise receipts, structured metadata, queued
  write-time enrichment, local recall, home-directory scope safety, and
  automatic cross-session context injection.

## 2.1.1 - 2026-07-17

- Hardened agent project scoping and made its state visible. MCP status now
  reports whether scope came from an explicit ID, Git remote, or local path,
  carries actionable warnings, and separates current-workspace memory count
  from the current user's total. Starting from the home-directory fallback now
  blocks project writes, project-memory injection, and transcript capture until
  the user starts Codex in the intended project or explicitly allows the
  fallback. Read-only global-memory hooks remain available. Confirmation uses
  `--allow-path-fallback-writes` or
  `NARRATORDB_ALLOW_PATH_FALLBACK_WRITES`. Non-home, non-Git projects remain
  writable with a machine-local scope warning.
- Tightened the MCP contract and user-facing interaction. `source` is now an
  explicit `user|assistant|system|memory` enum; tool results show concise human
  text while preserving structured metadata for clients. Remember, onboarding,
  and health skills avoid raw JSON and redundant calls; onboarding and health
  each use one read-only status call and rely on Codex's native progress UI.
- Pinned the plugin MCP server and offline hook wrapper to the same full Git
  commit. Once the MCP start has populated the `uvx` cache, offline hooks can
  reuse the immutable checkout without attempting symbolic Git-tag resolution.

## 2.1.0 - 2026-07-17

- Added the production local stdio MCP surface with six deliberately small
  tools: `remember`, `remember_session`, `recall`, `resume`, `forget`, and
  `status`. Server-start identity fixes the local user and project workspace;
  tools may use only that project or the user's global scope. Recall and resume
  remain local in both Private and Intelligence modes, and destructive deletion
  requires a specific message ID plus `confirm=true`.
- Added native `narratordb mcp install` and `mcp uninstall` workflows for Codex
  and Claude Code. Installation validates the optional MCP runtime and database
  mode, supports a read-only `--dry-run`, delegates registration changes to the
  client's own CLI, keeps credentials out of arguments, and preserves memory
  data on uninstall. New MCP databases default to Private; Intelligence must be
  configured with an explicit compiler first.
- Added the repository-distributed Codex plugin and marketplace entry. The
  plugin runs the `narratordb-memory[mcp]` GitHub distribution through `uvx`,
  bundles onboarding/remember/forget/health skills, and connects
  `SessionStart`, `UserPromptSubmit`, `PreCompact`, and `Stop` to bounded,
  fail-open local hooks. Hook capture excludes reasoning, tool output, system
  and developer context, applies best-effort secret redaction, strips inherited
  provider credentials, and can be disabled with
  `NARRATORDB_AUTO_CAPTURE=false` without disabling explicit MCP writes or
  local recall.
- Documented the mutually exclusive Codex installation paths, exact GitHub and
  marketplace commands, Private versus Intelligence behavior, project/global
  scoping, and the MCP tool contract. Bounded returned context can reduce input
  relative to full-history replay, but token savings are now stated only as a
  measured comparison against a declared baseline, never as a guarantee.
  Ecosystem notes identify concepts also present in Mem0, Zep, HydraDB,
  Exabase, and LangGraph without claiming API compatibility, benchmark parity,
  or unsupported superiority.
- Reconciled the benchmark plan against the original-author executable QA
  protocols rather than treating Mem0's fork as canonical. The documented
  primary comparable scope is all 1,986 LoCoMo QA items, LongMemEval-S all 500,
  and all 2,000 BEAM questions across four tiers, with each benchmark's
  prescribed scorer or judge. Added a dated, assumption-explicit API cost model
  separating Private and Intelligence configurations, the optional
  official LongMemEval-M scale track, first-pass compilation from cheaper evaluator
  reruns, and planning estimates from provider billing. The formulas, source
  revisions, price URLs, observed inputs, and exclusions are also tracked in a
  machine-readable planning record. No full-suite run, invoice, authorization,
  or spend is implied.
- Added a compact tracked record for the official-OpenAI GPT-5 short
  diagnostics. It preserves the 38/40 LoCoMo and 0.65458 BEAM results, exact
  scope and result hashes, provider/configuration disclosure, completeness,
  limitations, and the ignored raw bundle's 102-file manifest identity without
  checking databases or model-content artifacts into Git.
- Added a first-party `openai` Intelligence compiler. It is hard-pinned to
  `https://api.openai.com/v1/chat/completions`, reads only `OPENAI_API_KEY`,
  rejects custom endpoints and OpenRouter routing flags, uses strict structured
  output, verifies returned GPT model identity, and records content-free
  cached/reasoning token and cost telemetry. Focused official-route, schema,
  usage, budget, CLI, and benchmark-server coverage passes without model calls.
- Added first-party `gpt-5.6-luna` cost accounting at `$1/M` input,
  `$0.10/M` cached input, and `$6/M` output, including whole-request price
  multipliers above 272K input tokens. The historical OpenRouter
  `openai/gpt-5.6-luna-pro` identity remains separate and unchanged.
- Tightened lifecycle CLI validation, made the lexical stress regression
  independent of installed semantic extras, aligned its local/CI gate at all
  5,006 messages, added public `verify-index --records-only` ledger checks, and
  made historical admission tests independent of ignored local artifacts.
- Completed two frozen Mem0-harness short diagnostics through the official
  OpenAI API with GPT-5 requested for compiler, answerer, and judge. The
  compiler response retained the dated `gpt-5-2025-08-07` identity; the clean
  harness does not retain answerer/judge returned model IDs or usage, so those
  backend snapshots and total evaluator cost cannot be reconstructed from the
  artifacts. The balanced LoCoMo-40 run scored 38/40 (95.0%): multi-hop and
  temporal were 10/10, open-domain and single-hop were 9/10. The BEAM 100K
  conversation-0 run scored 0.65458 average rubric score with 15/20 passing
  (75.0%) and zero evaluation errors. A separate deterministic local
  recomputation, exact question-ID hashes, zero failed ingestion chunks, all 22
  compiler jobs, SQLite integrity, provider isolation, and secret scans passed.
  BEAM recovered three answer-generation timeouts on one ultimately perfect
  question. Compiler spend was $0.7860125 plus $0.3192825. These same-requested-
  model self-judged short diagnostics are not full-benchmark, blind-holdout,
  independent-judge, or Mem0 head-to-head claims. The tracked compact record
  binds the local ignored artifact bundle at
  `reports/scored-short-canaries-20260717/`.
- Added a separately identified `codex-cli` Intelligence compiler for
  ChatGPT-subscription experiments. It runs one fresh ephemeral, read-only,
  tool-disabled Codex task per session, pins and fingerprints the CLI/executable,
  accepts only strict JSONL plus schema-valid final output, reuses the V7
  source-grounding validator, strips API credentials through a child-environment
  allowlist, and fails closed on login, version, tool, quota, timeout, output,
  or grounding errors. CLI/server wiring, subscription usage accounting,
  invocation/concurrency fuses, and a content-safe three-session canary are
  covered by mocked no-model tests. The backend is disclosed independently of
  the OpenAI API and OpenRouter routes. The first live subscription canary then
  passed 3/3 sessions and 17/17 checks with no retry or recall-time upstream
  call, using exactly three tasks in 55.992 seconds; this is a compiler gate,
  not a benchmark percentage.
- Completed and checksum-sealed the V18 R3 direct-official-OpenAI paired
  LongMemEval-S dev42 diagnostic. Top-50 scores were 42/42 (100%) in the
  primary arm and 41/42 (97.6190%) in the unconditional replication, giving a
  paired floor of 97.6190%; top-20 scores were 39/42 and 38/42. Each arm used
  identical frozen prediction bytes and completed 168/168 answerer/judge calls
  (336/336 total)
  with zero transport transients, terminal rejections, or hidden retries. The
  new conservative token-ledger cost was $1.120186500. This remains explicitly
  classified as score-exposed, same-model-self-judged consumed-development
  evidence—not a blind holdout, independent-judge result, headline benchmark,
  or Mem0 head-to-head. The sanitized record and exact hashes are retained in
  `benchmark_records/narratordb_intelligence_dev42_v18_gpt54mini_official_r3_20260717.json`.
- Ended the R5 score embargo by explicit user direction and directly reran the
  frozen official evaluator offline, with zero model calls, network calls, or
  incremental spend. V13 scored 36/42 (85.7143%) at top 20 and 38/42
  (90.4762%) at top 50; its contemporaneous V7 control scored 36/42 and 37/42
  (88.0952%), respectively. Both reports are complete, validation-clean, and
  their canonical hashes exactly match the score-blind hashes precommitted in
  the two arm gates. V13 therefore gained one top-50 question (+2.381 points)
  but did not reach 95%. The result is development-consumed and provisionally
  published through the simpler direct path; the unfinished R3 finalization
  recovery remained unsealed and was not executed. Full provenance is retained
  in `benchmark_records/narratordb_intelligence_dev42_v7_v13_paid_pair_direct_score_20260717.json`.
- Added a fail-closed per-arm benchmark gate that runs the frozen-prediction
  evaluation audit before a later arm can be authorized. It preserves a
  read-only audit and exits nonzero on incomplete answers or judges, scope or
  denominator drift, validation findings, unknown costs, invalid identities,
  route allowlist drift, or a cost-cap breach. Recovered malformed responses
  remain disclosed retry telemetry rather than an automatic gate failure, and
  the persisted gate artifact omits score numerators, accuracy, verdicts, and
  model content.
- Hardened the paid evaluator's score embargo and error transport. Raw harness
  stdout/stderr is captured to an immutable evaluator log instead of being
  streamed before both arms authorize release. Upstream HTTP error bodies are
  still parsed privately for content-free cost/route accounting, but only a
  sanitized status and retry message can reach the harness or its log.
- Preserved R4 of the V7-control/V13 paid paired scorer as a terminal,
  score-embargoed evaluation failure. V7's process and route-identity checks
  exited cleanly, but the frozen score-blind completeness audit found four
  empty generated answers and two empty judge payloads, so V13 was not run and
  no R4 percentage is publishable. The content-free ledger exposed 30
  contentless GLM-5.2 responses, while the undersized $1.25 per-arm soft fuse
  produced 17 local 402 retry failures under ten-worker reservation pressure;
  settled V7 evaluator cost was only $0.92196287. The live transport now turns
  blank upstream 2xx completions into sanitized retryable 502 responses after
  recording their cost, and the replacement protocol requires a production-
  sized arm fuse plus a zero-empty completeness gate before the second arm.
  R4 remains immutable evidence and must never be resumed or repaired.
- Added the V12 generic aggregation correction after the V11 paired postmortem.
  Present-perfect cumulative snapshots such as “how many times have I/we” now
  retain ordinary current-state ranking instead of being flattened into a
  distinct-event pack. True aggregation packs prioritize directly matched
  claim sources, conservatively select event/fact evidence using claim text,
  structured subject/predicate/object fields, and memory keys, omit unrelated
  co-located numeric facts, and keep a bounded dense/raw fallback when no
  query-backed claim exists. The pack identity format is v2. Ten synthetic
  aggregation tests cover snapshot parity, independent acquisitions,
  complementary quantities, irrelevant facts, vocabulary gaps, filtering,
  provenance, retellings, and non-aggregation parity without benchmark data.
- Completed and preserved the precommitted V7-control/V11 consumed-dev paired
  evaluation. The contemporaneous V7 control scored 35/42 (83.3333%) at top 20
  and 38/42 (90.4762%) at top 50; V11 scored 35/42 and 37/42 (88.0952%), so it
  is not promoted. Both full evaluations passed integrity and route/cost audits,
  with no dropped questions, empty answers or judges, frozen-payload mismatch,
  unknown cost, or invalid completion identity. Exact comparison found 33/42
  byte-identical retrieval payloads. Three of five verdict flips—including the
  net top-50 loss—occurred on identical contexts and are answerer/judge variance;
  aggregation itself produced one cross-session count gain and one evolving
  cumulative-state loss. The complete negative result, $1.352669714 paired
  evaluation cost, hashes, and generic V12 correction are retained in
  `benchmark_records/narratordb_intelligence_dev42_v7_v11_paired_20260716.json`
  and its postmortem.
- Bumped the synthetic Intelligence canary report to v4 after preserving a V10
  false-negative. OpenRouter returned the exact requested model and full
  provider route for all three successful synthetic calls but omitted its
  optional internal attempt list. Missing optional attempt history is now
  reported as unavailable and is non-gating; every attempt that is present
  remains strictly checked against the frozen provider allowlist. Exact final
  model/provider attestation remains mandatory and fail-closed. The frozen V11
  rerun then passed 3/3 jobs and all 16 checks on exact model `z-ai/glm-5.2`
  and final provider `parasail/fp4`, with zero recall-time model calls and
  `$0.01658584` total cost. This was a synthetic transport/compiler gate with
  no benchmark questions or accuracy score.
- Added a generic, bounded aggregation evidence pack for total, count, and
  cumulative-history questions. It selects source-linked user events and facts
  across distinct sessions, preserves chronological and quantity groupings,
  labels possible retellings without counting them, and performs no hidden
  arithmetic or query-time model call. Ordinary and current-state retrieval
  keep the existing ranker. Filtered recall now scopes raw messages, compiled
  claims, and every evidence-pack source to the same filter, closing an
  unfiltered-claim escape path.
- Hardened hosted compilation for production-shaped rate limits. Semantic
  repairs and wire retries now have separate disclosed limits; every wire start
  shares one pacer, honors bounded `Retry-After` metadata, and records a
  content-free success or error attempt. Retry eligibility is durable in the
  derived job schema, ordered ZDR provider allowlists are strict and
  fingerprinted, route attestations use closed metadata, and malformed
  provider responses cannot write arbitrary upstream text to usage reports.
  HTTP protocol failures now retry as content-free ledger events. Successful
  responses with unverified model or provider identity fail closed before
  reaching a scorer. Missing upstream billing consumes a positive conservative
  request reservation, remains explicitly marked as unknown, and blocks a
  publication-ready budget or evaluation audit until reconciled.
  Project-config schema v2 is a downgrade barrier for the new retry semantics.
  Local request reservations remain a single-process soft fuse; an external
  provider/account cap is still required as the aggregate backstop.
- Added the query-local V8 claim renderer. Ranked compiled claims now
  expose bounded typed subject/predicate/object/key/time fields plus up to three
  source-grounded evidence excerpts, without a query-time hosted model or
  arithmetic inference. Memory-key format v2 canonicalizes harmless path and
  word-separator drift, migrates legacy keys per user, and deterministically
  consolidates same-slot active state while retaining the superseded timeline.
- Added a fail-closed existing-derived benchmark replay mode for isolating
  local retrieval/render changes from already-paid compiler output. The replay
  constructs no compiler, cache, or usage ledger; rejects every exposed
  mutation; and permits search only when every current registered source has a
  terminal `complete` or disclosed `partial` job under an explicitly supplied
  compiler fingerprint. The frozen V8 replay protocol remains a consumed-dev42
  diagnostic, never a fresh end-to-end or held-out score.
- Added a content-free campaign budget auditor and declaration for the current
  95% development effort. It rejects duplicate sources, credentials, model
  content, malformed ledgers, and non-finite costs; enforces the explicit $250
  USD provider cap; and records the separate EUR 300 governance ceiling without
  inventing an exchange rate.
- Hash-precommitted the full BEAM-1M and LongMemEval-V2 dataset scopes before
  downloading their answer-bearing data. Both artifacts keep execution
  unauthorized until a second protocol freeze supplies the implementation,
  adapter, models, failure denominator, budget, and success rule. BEAM-1M is
  labeled NarratorDB-unrun rather than locally unseen because vendor result
  files already exist; LongMemEval-V2 is public-answer and blind to this
  development process, not globally secret.
- Preserved content-free reasons for terminal partial compiler jobs and exposed
  aggregate `partial_reasons` through enrichment status. Hosted content filters
  and refusals remain fail-closed and non-retryable: canonical raw memory stays
  searchable, while NarratorDB does not automatically subdivide source content
  to route around a provider decision. The recovery and fair-publication policy
  is documented in `docs/content-filter-recovery.md`.
- Upgraded the query-independent compiler prompt to V7. Each session may use a
  deterministic local selection of prior active claims as untrusted hints for
  stable memory-key reuse, while current canonical messages remain the only
  allowed evidence. Reference context is included in compiler cache format v3,
  and salient assistant recommendations, resources, plans, and commitments are
  retained when they matter for future continuity.
- Reworked Intelligence top-k retrieval with weighted raw/claim rank fusion,
  source-support reinforcement, bounded query-anchored session neighborhoods,
  canonical-message deduplication, and ordinal/URL-preserving excerpts.
  Candidate count is independent of render budget and filtered searches cannot
  escape their filters through sibling expansion.
- Added query-free, idempotent benchmark finalization plus explicit
  `finalization_ms` and `lazy_finalized_sessions` timing fields. The unchanged
  official harness keeps a labeled lazy compatibility flush; lifecycle-aware
  clients can materialize pending sessions before search.
- Removed the experiment-specific `$180` default from the production facade.
  Operators can set `NARRATORDB_COMPILER_MAX_COST_USD`; paid benchmark runners
  require their own explicit disclosed spend fuse. Both use a cumulative local
  USD ledger stop that is soft for an in-flight call; an external account cap
  remains the aggregate multi-process/multi-run backstop.
- Added the isolated three-call `narratordb-intelligence-canary`. It uses fake
  fixtures and a fresh temporary database, cache, and usage ledger to check
  route observations, grounding, prior-claim updates, stable-key supersession,
  structural recall, cache behavior, database health, and zero model traffic
  during recall. Its optional JSON report contains configuration, aggregate
  usage, phase timings, route observations, and booleans but no prompts, source
  content, claims, queries, or recalled text; a positive explicit cost stop is
  mandatory.
- Preserved the first GLM-5.2/DeepInfra V8 synthetic canary as a failed,
  zero-event, zero-cost transport attempt. The pinned ZDR endpoint metadata
  advertised `max_tokens`, while the prior strict adapter forced
  `max_completion_tokens`; a generic persisted output-token parameter repair
  was hash-precommitted before retry without changing prompts, memory schema,
  or retrieval.
- Added a third-party benchmark reproducibility policy requiring clean empty
  state; frozen source, dataset, split, harness, dependency, and model settings;
  complete commands; raw prediction/evaluation artifacts and manifests; and
  cost/latency disclosure. It forbids hidden caches and pre-saved answers and
  distinguishes bit-for-bit archive verification from statistical fresh-run
  reproducibility under nondeterministic hosted models. This policy does not
  promote a prediction freeze by itself to an answer/judge score.
- Added `narratordb-benchmark-reproduction` with `preflight`,
  `verify-preflight`, `seal`, and `verify-seal` commands. It records and
  recomputes immutable inputs and fresh-state assertions, rejects credential
  material, binds the declared configuration and ordered argv, and secret-scans
  and hashes the completed artifact tree without implying deterministic hosted
  completions.
- Added model-aware hosted defaults: GPT-5.4 Mini remains Azure/`minimal`, while
  Luna Pro uses Azure/`low`, its lowest currently advertised nonzero reasoning
  effort, instead of inheriting an unsupported `minimal` request.
- Archived the complete GPT-5.4 Mini compiler V6 development run: 29/42
  (69.0476%) at top 20 and 31/42 (73.8095%) at top 50. A post-hoc same-ID slice
  of immutable NarratorDB 1.3 outputs scored 37/42 (88.0952%) and 36/42
  (85.7143%). This is an inspected development diagnostic, not a holdout or
  Mem0 comparison; the later V7 comparison remains development-only. The
  engine worktree was dirty, so the archive freezes its commit, tracked diff,
  and source manifest alongside the database and compiler-cache hashes.
- Froze the fresh GPT-5.4 Mini compiler V7 dev-42 prediction phase before any
  answerer or judge call. All 42 scoped predictions and ingestion artifacts are
  hash-verified and the freeze was explicitly unscored with evaluator status
  `not_started` at that checkpoint. The compiler ledger
  contains 1,983 events costing $33.14190525 on the exact Azure route: 1,976
  complete jobs and three disclosed provider-content-filter partials retain raw
  fallback, with no dropped question or selective retry. One harness timeout
  produced one preserved BrokenPipe request event before the normal retry.
- Evaluated byte-identical copies of the frozen V7 and V8 replay predictions
  under a paired protocol committed before either score. V7 scored 36/42
  (85.7143%) at top 20 and 38/42 (90.4762%) at top 50; the consumed-development
  V8 local retrieval/render replay scored 34/42 (80.9524%) at both cutoffs and
  is not a fresh end-to-end or held-out result. The unchanged harness retried
  eight V7 and six V8 malformed HTTP-200 responses; both final 42-question
  results have no empty answers or judges. The first V8 replay preflight is
  preserved as superseded before execution, and the executed run is bound to
  attempt 2.

## 2.0.0 - 2026-07-15

- Added explicit `private` and `intelligence` project modes. New databases
  require a choice; existing 1.x databases migrate to private mode without
  enabling model traffic.
- Added a query-independent session compiler with strict structured output,
  exact source grounding, entities, atomic claims, temporal fields, stable
  memory keys, and cross-session supersession chains. Hosted output is bounded
  at 8,192 tokens by default; exact duplicate claims, same-session updates,
  malformed optional dates, and ungrounded derived items degrade
  deterministically while canonical raw messages remain searchable.
- Added loopback-only local compilation and a privacy-pinned OpenRouter path.
  Hosted credentials remain runtime-only; provider routing disables fallbacks,
  denies data-collecting endpoints, requires ZDR, and records content-free
  usage behind a configurable cost stop. GPT-5.4 Mini is the hosted default;
  Luna Pro remains an explicit selectable experiment.
- Added a secure persistent compiled-session cache with evidence rebinding,
  cross-process singleflight leases, stale-owner recovery, and deletion-aware
  invalidation.
- Added resumable enrichment jobs, retry/exhaustion handling, stale-worker
  recovery, compiler-fingerprint invalidation, bounded intelligence recall,
  hybrid raw/claim retrieval, derived-data status, backfill, and purge APIs.
- Added intelligence-mode support to the official benchmark adapter while
  keeping private/raw behavior as the default. Restored hash-pinned development
  and holdout splits plus prediction/evaluation integrity audits and CI gates.
- Bumped the Python package and public API version to 2.0.0 and expanded unit,
  concurrency, packaging, installed-wheel, high-noise, LoCoMo, and scale tests.

- Added a complete engineering-first NarratorDB dashboard preview. A responsive
  application shell now exposes overview metrics, canonical record inspection,
  entities and scopes, a deterministic retrieval lab, request activity,
  integrations, API keys, team access, usage, and settings. Search, filters,
  bulk record actions, inspectors, retrieval runs, key rotation/revocation,
  invitations, role changes, and settings behave as session-scoped simulations
  with explicit preview labeling and a safe reset path. Orange data marks,
  green operational states, bounded charts, compact tables, keyboard focus,
  reduced-motion handling, and mobile navigation extend the established
  white-and-black commercial system without implying live cloud control.

- Replaced the homepage's flat stripe animation with a contained grayscale 3D
  memory field. A deterministic canvas projection now renders clustered nodes,
  depth-aware connections, anchor halos, and soft fog in the hero's deliberate
  upper-right space, with restrained pointer parallax that never displaces the
  copy. Rendering is device-pixel-ratio bounded, suspends offscreen, and draws
  one stable composition when reduced motion is requested.

- Added a full-width automatic NarratorDB memory showcase to the homepage.
  Personal Assistant, fictional Care Coordination, and Customer Support
  scenarios now type source conversations, build source-linked scoped records,
  retrieve evidence, and return provenance-backed context through one
  deterministic Ingest/Build/Retrieve timeline. Scenarios cycle while visible,
  restart on tab selection, support direct stage seeking and keyboard tab
  navigation, suspend offscreen, and become static under reduced motion. The
  experience intentionally has no play, pause, replay, arbitrary input, or live
  cloud dependency.

- Completed a site-wide legibility and breathing pass without changing the
  established section palette or page architecture. Visible interface type now
  has an automated 11 px minimum, supporting copy sits at a calmer 14–16 px,
  light fields use stronger near-black secondary text, and ink fields use
  brighter grays. Research charts, provider records, pricing cards, and access
  forms gained wider text columns, relaxed rhythm, wrapped metadata, and
  mobile-safe label/value geometry. Reveal effects now use transform only, so
  content cannot disappear while client-side motion initializes.

- Returned the homepage Research field and the research question-type view to
  monochrome surfaces so orange comparison marks remain dominant. Refined the
  benchmark grids with clipped tracks, bounded fills, fixed tabular score
  columns, a non-bleeding selected state, safer efficiency-plot bounds, and
  more breathing room across category labels, bars, and values.

- Added an avant-garde section-color system to stop the multi-page commercial
  site reading as one continuous black-and-white sheet. Acid chartreuse,
  editorial pink, safety coral, industrial silver, and inverted ink now define
  full content fields, while slim palette rails connect interior heroes,
  section boundaries, benchmark proof cells, and closing calls to action. Blue
  and cream remain excluded; chart marks stay orange and live actions stay
  green.

- Polished the commercial website into an enterprise-tier cloud product system:
  explicit page routes, sticky research/product section indexes, larger section
  separation, consistent layout tokens, restrained one-time reveals, keyboard
  focus states, responsive provider disclosures, and a lower-density provider
  index. The white/black visual language, orange charts, green live actions,
  bottom-left hero composition, and contained gray smoke treatment remain.
- Repositioned commercial pages around proprietary NarratorDB Cloud while
  preserving NarratorDB 1.3's historical open-source record in repository
  documentation. Pricing now presents Free, Builder, Pro, and Enterprise cloud
  tracks as preview targets rather than shipping commitments.
- Added a real adaptive cloud-access flow for personal, team/startup, and
  enterprise requests. Explicitly consented leads are validated and upserted in
  Cloudflare D1 without collecting IP addresses or browser fingerprints; the
  generated migration and API contract are covered by the site test suite.
- Published a sanitized first-party LongMemEval JSON record from the site with
  frozen score, ingestion, latency, cost, integrity hash, and non-comparability
  fields, so NarratorDB evidence no longer depends on a commercial-page GitHub
  link.

- Rebuilt the product website around five canonical routes: homepage, product,
  research, pricing, and early access. Removed legacy pages now redirect to the
  relevant canonical section, while shared navigation, mobile behavior,
  persisted form validation, pricing cadence, and reduced-motion fallbacks
  remain intact.
- Added a sourced research directory for NarratorDB, Mem0, Zep, HydraDB,
  Hindsight, Supermemory, Exabase, Mastra, LangMem, and Letta. Interactive
  LongMemEval, LoCoMo, and BEAM views retain the published model, cutoff,
  configuration, infrastructure, verification date, evidence gaps, and primary
  source. Cross-vendor figures remain explicitly non-identical rather than a
  controlled leaderboard.
- Replaced the cream/blue presentation with a minimal white-and-black editorial
  system. Comparison bars use orange consistently; primary actions and
  available/healthy states use green, with solid live-state badges distinct
  from neutral planned states. The homepage hero keeps deliberate empty space,
  bottom-left typography, and a layered gray smoke animation contained by its
  border, with a static reduced-motion fallback.
- Fixed benchmark value overflow by reserving a dedicated score column at
  desktop and mobile widths. The production build, lint, five rendered-route
  suites, redirect checks, research integrity checks, pricing/status checks,
  and 404 handling pass. The current private deployment is
  `https://narratordb-home.william615395.chatgpt.site`.
- Clarified the licensing direction across the website and documentation:
  NarratorDB 1.3 retains its historical MIT license, while the planned hosted
  platform and future commercial releases are proprietary. No historical
  license text or released-package metadata was retroactively changed.
## 1.3.0 - 2026-07-15

- Fused the search reranker into one intent-gated pass: all query-intent and
  evidence regexes compile once at import, each candidate row is lowered,
  tokenized, and pattern-matched exactly once, and evidence patterns run only
  when the corresponding query intent consumes their result. On the rebuilt
  full-500 LongMemEval corpus (245,780 messages) the uncontended
  official-shape search dropped from 40.31 ms to 12.94 ms mean.
- Added retrieval-time result shaping on ranked direct hits: near-duplicates
  of a higher-ranked hit (normalized-terms Jaccard >= 0.90) demote to the
  tail instead of dropping, sessions cap at 4 hits inside the top-20 window,
  current/before intents pick the latest/earliest member of a duplicate
  cluster, and a relative confidence floor (0.25 x top score, never below 20
  results) trims the weak tail. A full-500 ablation pinned the floor so
  evidence coverage at the declared top-20/top-50 cutoffs exactly matches the
  unshaped baseline while the median result list shrinks from 37 to 28.
- The benchmark adapter now merges position-adjacent same-session hits into
  one memory block (gap <= 1, merged text capped at 1,200 characters, unmerged
  hits never truncated), configurable via --no-merge-adjacent, --merge-gap,
  and --merge-max-chars. SearchResult carries the real fused score per direct
  hit, and the adapter exposes them in query_debug.
- Serialized SentenceTransformer encode calls on a process-wide lock:
  concurrent encodes from HTTP worker threads can segfault on Apple MPS and
  were the cause of silent benchmark-server crashes under 8-10 worker load.
- Added a bounded per-engine query-embedding LRU, pre-warmed each scope's
  embedding matrix after benchmark ingestion, and enabled a 2 GB SQLite mmap
  window so multi-scope read workloads share the OS page cache instead of
  paying per-connection cold reads (the dominant cost of the published
  769.70 ms official-path mean).
- Fixed the stress-suite stdio bridge to launch with sys.executable instead
  of resolving python3 through a stripped environment, and added a
  golden-ordering parity suite plus shaping contract tests.
- Documented the NarratorDB 1.3 optimization sequence: keep the completed
  all-500 and 458-question evaluation split locked, tune only on declared
  development, LoCoMo, and answer-independent synthetic data, then freeze the
  engine before one evaluation-only rerun.
- Completed the frozen all-500 evaluation under the unchanged official
  harness with GLM 5.2 answering and dated DeepSeek V4 Flash judging at the
  pre-declared cutoffs 20 and 50: 408/500 (81.6%) at top 20 and 414/500
  (82.8%) at top 50, up from 78.0%/76.0% in 1.2, at $9.98 total including
  the 30-question pre-flight. Official-harness HTTP search latency fell
  from 769.70 ms mean / 1,458.8 ms p95 to 30.57 ms / 86.1 ms. The result
  does not beat Mem0's published 94.8% GPT-5 top-50 reference and the
  non-identical answerer/judge keeps claim_beats_mem0_accuracy false. Full
  record: benchmark_records/narratordb_full500_glm52_20260715.json.

## 1.2.0 - 2026-07-14

- Added a strict evaluate-only auditor that recomputes every cutoff and
  question-type score, rejects missing answers or judge outputs, verifies the
  frozen retrieval payloads did not change, and reconciles provider, token,
  error, and cost ledgers.
- Added a declared low-cost OpenRouter provider allowlist with automatic
  same-model fallback, plus resume-safe accounting that restores prior usage
  before enforcing the run's cost cap.
- Added content-free malformed-response and official-harness retry diagnostics,
  including separately audited full, development, and untouched-holdout views.
- Completed the frozen official LongMemEval all-500 V4 Flash answer-and-judge
  run: 73.6% at top 10, 78.0% at top 20, 76.0% at top 50, and 76.6% at top 200.
  The untouched 458-question holdout reached 77.73% at top 20.
- Archived the exact command, source and dataset hashes, all official outputs,
  independent score/output audits, provider routing, token ledger, and
  $2.615917 final-run cost. Two empty generated answers remain valid FAILs
  in the official denominator rather than being repaired or cherry-picked.
- Added evidence-conditioned answer attribution: at top 50, V4 Flash passed
  81.96% even when every labeled evidence session was retrieved, identifying
  answer/judge/context reasoning as a remaining accuracy bottleneck.

## 1.1.0 - 2026-07-14

- Added schema v2 with per-user persisted normalized terms, safe legacy
  backfill/repair, and health-check coverage.
- Added bounded stemming cache and eliminated repeated tokenization on storage,
  FTS rebuild, lexical checks, and reranking.
- Added per-stage retrieval timings and per-user benchmark concurrency; the
  archived 30-question median improved from 559.4 ms to 43.53 ms while all
  labeled evidence sessions remained inside top 10.
- Corrected temporal-intent ranking when a query asks for a current fact but
  uses words such as "previous" only to describe the subject.
- Added generic vocabulary-gap rescue and current count/state ranking, with
  measurement and interval safeguards.
- Added a content-free, cost-capped OpenRouter benchmark transport with pinned
  provider and reasoning controls.
- Added append-only benchmark history, manifest/secret verification, archived
  retrieval replay, competitor-reference auditing, and cost tracking.
- Completed and integrity-audited the frozen official LongMemEval all-500
  predict-only run: 124,345 conversation pairs, 245,780 stored messages, zero
  failed pairs, and a separately reported untouched 458-question holdout.
- Recorded 95.41% top-10 and 97.60% top-50 exact evidence-session recall-any on
  that holdout, with 772.62 ms mean and 1,486.2 ms p95 official HTTP search
  latency; these are explicitly retrieval metrics, not answer/judge accuracy.
- Expanded continuous integration across Python 3.10-3.14, with a separate
  full provenance, typed-memory, code, LoCoMo, and scale stress gate.
- Improved the Mem0 categories 1-4 LoCoMo answer-in-context regression from
  1,297/1,540 to 1,301/1,540 while reducing mean retrieval latency from 39.83 ms
  to 6.36 ms. This internal subset excludes the original-author category-5
  adversarial items.

## 1.0.0 - 2026-07-14

- Established NarratorDB as an independent package and data namespace.
- Added versioned SQLite/FTS metadata and deep integrity checks.
- Added consistent online backups with post-copy verification.
- Hardened cleanup across messages, embeddings, provenance, typed records, and
  relations.
- Added restart, backup/restore, concurrent-writer, and benchmark-contract tests.
- Added official LongMemEval harness interoperability and transparent
  LongMemEval/BEAM retrieval diagnostics.
