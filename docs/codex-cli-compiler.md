# Codex CLI subscription compiler

NarratorDB can use a ChatGPT-authenticated Codex CLI process as the optional
write-time compiler for Intelligence mode. This is a distinct compiler backend,
`codex-cli`; it is not labeled as the OpenAI API compiler and it does not make
recall-time model calls.

Each session is compiled by a fresh, ephemeral `codex exec` task in an empty
working directory. NarratorDB supplies the existing V7 compiler prompt and JSON
Schema on standard input/files, removes API credential variables from the child
environment, disables user configuration and repository rules, selects a
read-only sandbox, and rejects any run that reports tool use or does not return
schema-valid, source-grounded memory JSON. The persistent NarratorDB compiler
cache remains the only reuse layer.

## Preconditions

Install the repository environment, then verify the executable and its ChatGPT
login without putting any API key on a command line:

```bash
cd /path/to/narratorDB
.venv/bin/python -m pip install -e .
codex --version
codex login status
```

Freeze the exact Codex CLI version, model, reasoning effort, NarratorDB source
revision, compiler fingerprint, dataset, and commands in any benchmark record.
The initial profile is `codex-cli 0.144.4`, `gpt-5.4-mini`, reasoning `low`, one
concurrent process, and a 300-second hard timeout. A floating model alias and
subscription serving can vary between runs, so fresh results are statistically
reproducible rather than promised to be byte-identical.

## Promotion sequence

Never begin with the full LongMemEval ingestion. Use these gates in order:

1. Run the isolated three-session synthetic canary with a fresh database and
   compiler cache. It must finish all three jobs, make zero recall-time upstream
   calls, pass every grounding/update/cache check, and stay within its invocation
   fuse.
2. If the synthetic gate passes, run a bounded 50-100-session throughput sample
   from a frozen, score-blind scope. This estimates subscription quota pressure
   and elapsed time; it is not an accuracy score.
3. Build the fresh dev-42 memory database. The known scope contains 1,979
   coalesced conversation sessions, so set and disclose an invocation fuse that
   covers the chosen semantic retry policy. Keep the compiler cache and
   content-free usage ledger so the build is resumable and auditable.
4. Run the official answerer/judge evaluation only after the build passes its
   completeness audit. The score is a consumed-development diagnostic.
5. Freeze the implementation and protocol before a fresh full-500 build. The
   full scope contains 23,745 coalesced sessions. Archive the clean-state
   preflight, usage ledger, predictions, evaluations, audits, and manifests.

The invocation fuse protects subscription quota; the OpenRouter USD fuse does
not apply to this backend. Subscription-backed usage is recorded with zero
marginal API cost and an explicit `subscription` cost source. It must not be
combined with API-billed compiler cost as though the two routes were identical.

## Three-session canary

With `codex` on `PATH` and `codex login status` reporting ChatGPT authentication,
run:

```bash
mkdir -p reports/codex-cli-canary
unset CODEX_API_KEY CODEX_ACCESS_TOKEN OPENAI_API_KEY OPENROUTER_API_KEY

.venv/bin/python -m narratordb.benchmarks.intelligence_canary \
  --compiler codex-cli \
  --model gpt-5.4-mini \
  --reasoning low \
  --codex-cli-version 'codex-cli 0.144.4' \
  --codex-timeout-seconds 300 \
  --codex-max-invocations 6 \
  --codex-max-concurrency 1 \
  --semantic-max-attempts 2 \
  --min-request-interval-seconds 0 \
  --report reports/codex-cli-canary/report.json
```

The process exits zero only when `status` is `passed`. The six-invocation fuse
allows at most two semantic attempts for each of the three sessions; a normal
first-attempt pass uses three. The report is content-free and contains the
compiler fingerprint, route identity, token totals, zero marginal API cost,
timings, cache/job counts, and boolean checks. It contains no source messages,
claims, prompts, recalled text, CLI output, or credentials.

## Benchmark server

After a canary and bounded throughput gate pass, the official harness-facing
server uses this shape:

```bash
python3 -m narratordb.benchmark_server \
  --host 127.0.0.1 \
  --port 8889 \
  --database /fresh/run/intelligence.db \
  --mode intelligence \
  --compiler codex-cli \
  --model gpt-5.4-mini \
  --reasoning low \
  --codex-executable /absolute/path/to/codex \
  --codex-cli-version 'codex-cli 0.144.4' \
  --codex-timeout-seconds 300 \
  --codex-max-concurrency 1 \
  --codex-max-invocations 4000 \
  --compiler-semantic-max-attempts 2 \
  --coalesce-sessions \
  --context-token-budget 6000
```

`4000` is an example dev-42 fuse with retry headroom, not the full-500 value.
Use a new run directory and a predeclared value for every scored attempt. Do not
point a new headline run at a database or compiler cache created by development
questions.

The unchanged official harness then talks only to the local server. Prediction
and paid answerer/judge evaluation remain separate phases, as documented in
[`BENCHMARKS.md`](../BENCHMARKS.md). A Codex subscription compiler result must
disclose its backend separately from the answerer and judge models.
