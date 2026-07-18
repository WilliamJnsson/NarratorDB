# Content-filter recovery and publication policy

## Current fail-closed behavior

When a hosted compiler returns `content_filter` or a model refusal, NarratorDB
does not resubmit altered source content. The compiler job becomes terminal
`partial`, canonical raw messages remain committed and locally searchable, and
no ungrounded derived claims are created. Repeating ingestion or finalization
for the same source hash and compiler fingerprint is idempotent and makes no
new compiler call.

Partial jobs now retain their first content-free reason in the existing
`last_error` field. `enrichment_status()` exposes aggregate `partial_reasons`,
so an operator can distinguish a provider filter from schema-validation loss
without logging source or completion text.

## Why hosted subdivision is not the default

Automatically bisecting and resubmitting a filtered conversation is not a
safe transparent retry:

- it can route around a provider safety decision;
- a partition no longer has full-session conversational semantics;
- cross-partition updates and relations can be lost or misordered;
- claim, entity, evidence, and relation identifiers require a new deterministic
  merge protocol and global output caps;
- one request can expand into several paid requests and weaken a cost stop;
- a post-hoc retry policy changes the memory system being benchmarked.

For these reasons, `content_filter` and model refusal remain non-retryable.
Mechanical failures such as an incomplete or oversized completion are a
different class and may eventually use bounded subdivision, but that policy
must never apply to provider safety outcomes.

## Predeclared future recovery modes

A future version may add an explicit recovery policy to project and benchmark
configuration. The policy must be selected before ingestion and included in
the compiler lineage fingerprint.

1. `raw_only` remains the default. It preserves the current terminal `partial`
   behavior and performs no second model request.
2. `local_fallback` may invoke a separately configured loopback-only compiler.
   It must never forward a filtered session to another hosted route. The
   fallback compiler fingerprint, prompt version, request count, usage, and
   outcome must be stored on the job.
3. `bounded_split` may be considered only for declared mechanical errors. It
   must use deterministic contiguous turn boundaries, a strict subrequest and
   cost limit, the same source hash and reference snapshot, exact per-partition
   evidence validation, collision-free derived IDs, global output caps, and a
   distinct recovered-partial status. It must not claim equivalence to a
   successful full-session compilation.

The job identity for any recovery-capable version must bind:

- canonical source hash;
- primary compiler fingerprint and prompt version;
- recovery-policy version;
- fallback compiler fingerprint, when present;
- deterministic partition algorithm version, when present.

One job lease owns the primary and recovery sequence. Every upstream attempt
is accounted against the same cost ledger, and a restart must either resume
the exact declared stage or return the already-terminal result without a new
call.

## Offline test matrix for a recovery-capable version

- A filtered primary call makes no second hosted call under `raw_only`.
- Raw recall remains available, the job is `partial`, and
  `partial_reasons.content_filtered` increments.
- Repeated finalization makes zero new calls, including after restart.
- Reports and status payloads contain reason codes and usage only, never source,
  prompt, completion, evidence quote, or credential text.
- `local_fallback` rejects non-loopback endpoints and is impossible unless
  explicitly configured before ingestion.
- Changing policy, fallback compiler, prompt, or partition version creates a
  new lineage instead of reusing an incompatible cache entry.
- Fallback claims with invalid or cross-partition evidence are discarded.
- Concurrent workers cannot run the same recovery stage twice.
- The cumulative cost stop covers all primary and recovery requests; an
  in-flight-call overshoot remains disclosed.
- Mechanical splitting is deterministic, bounded, order preserving, and never
  triggered for `content_filter` or refusal.

## Fair benchmark publication

A run containing a filtered compiler session may be published only with its
complete denominator and the exact count and reason of partial jobs. It must
not be described as a clean all-complete compiler run. Canonical raw fallback
means the run can still produce a legitimate score, but readers must be told
that one or more sessions lacked normal derived enrichment.

Do not selectively retry, replace, delete, or exclude a filtered session after
questions or scores are visible. A clean headline result requires a new full
run from empty state under a frozen, predeclared recovery policy. Model
comparisons must use the same policy and publication rules. The original
partial run remains immutable and independently auditable.
