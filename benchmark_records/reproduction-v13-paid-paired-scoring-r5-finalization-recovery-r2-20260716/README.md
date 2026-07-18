# R5 offline finalization recovery r2 — UNSEALED CANDIDATE

This candidate does not repair, resume, or relabel the terminal R5 attempt, and
it does not retry recovery r1. R1 is permanently terminal after its exact sealed
launcher failed in preflight at line 39 with exit 67 because
`/usr/bin/realpath` was absent. It reached no worker, sandbox, scratch, output,
score, or Stage A path. R2 uses the completed immutable paid-arm evidence in a
new offline-only recovery protocol. The original attempt remains a terminal
finalization failure.

R2 recursively binds the immutable r1 terminal record
`74da98f221adc936c1ee39736ec9030bb6f4d9be2a34e823d2d7b1b94c24e0fe`,
its checksum manifest
`5274cb68ee5ab1ad612638102be9e1c0ca6d896e0165c752884162f0a273b829`,
and the physical r1 seal manifest
`43edeb0561dd45586771af764d77d97c539df7b124f91938dfa552af2a3349a1`.
The r1 bundle remains sealed and must not be modified or invoked again. Any R2
failure would require a separately reviewed and sealed r3 bundle.

Do not execute `stage-a` or `stage-b` while this directory is unsealed. Only the
non-publishing `preflight-smoke` and zero-seal launcher canary are authorized
before sealing. The recovery output root must remain absent until an
independently reviewed `SHA256SUMS` has been created, made immutable, and its
physical-file SHA-256 has been published externally.
This candidate has 11 physical files before sealing and exactly 12 after adding
`SHA256SUMS`. No R2 Stage A has run, and no R2 review, aggregate GO, recovery
output, scratch, or terminal-status artifact exists.

R2 uses fresh paths that are distinct from both the original R5 and r1 paths:

- bundle: `benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r2-20260716`
- output: `reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-finalization-recovery-r2-20260716`
- reviews: the two `.review-1.json` and `.review-2.json` siblings of the R2 bundle
- aggregate GO: the `.RECOVERY_GO.json` sibling of the R2 bundle

## Pinned launcher environment

The launcher uses `/bin/realpath`, not the absent `/usr/bin/realpath`. The exact
binary is pinned to SHA-256
`22cd7804166170874dddd490e146936b6957dbbbb93d14eacd86b8d0de3e3989`
and Apple code-signing identifier `com.apple.realpath`. Every absolute external
executable used by the launcher is exhaustively recorded with its path, hash,
file facts, and code-signing policy in `launcher-executables-r2.json`; the
launcher and tests require exact set equality rather than accepting an ambient
`PATH` substitute.

Before sealing, `run_offline_recovery.sh preflight-smoke` exercises the complete
clean launcher bootstrap and exact sandbox/worker boundary with disposable
private paths. It must return 0 with zero stdout/stderr and must create no R2
output. The zero-seal canary invokes `stage-a` with a 64-zero public seal. It must
reach the missing-seal check and return 71 with zero stdout/stderr, no scratch,
no bytecode, and no R2 output; this catches a regression to r1's earlier exit 67.

## Fixed ordering

The runtime keeps umask `0077` while the exact sealed R5 verifier creates its
private historical reconstruction directories. Computed output remains captured
in memory or in a private `0700` scratch directory with `0600` files. Only after
the full closed-world recomputation passes does the worker publish a new file
using `O_CREAT|O_EXCL|O_NOFOLLOW`, fsync it, fchmod it to `0444`, fsync it again,
and fsync its parent directory.

No recovery action receives a credential or may call a model, judge, provider,
or FX endpoint. The launcher supplies a five-key clean environment and a
deny-default macOS sandbox. Only the exact Python executable may be launched;
all network families are denied; repository inputs are read-only; writes are
limited to a private scratch directory and the new recovery output root.
The only Darwin system-query capability is `sysctl-read`, required because
Python `ctypes` calls `os.uname()` during import. The broader-looking
`system-info` capability was tested and did not permit that operation.

## Stage A (only after seal publication)

After sealing, export the public (non-secret) SHA-256 of the physical recovery
`SHA256SUMS` file as `NARRATORDB_R5_RECOVERY_R2_PRECOMMIT_SHA256`. From the exact
repository root, run:

```text
benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r2-20260716/run_offline_recovery.sh stage-a
```

The command emits zero stdout and stderr bytes. It reconstructs the historical
authorization, audit, both arm gates, ledgers, campaign, provider reconciliation,
and ECB/EUR gate at the preserved independent-review time. It publishes one
hash-only historical envelope. Reconstructed documents and the raw verifier
report remain in memory and are represented only by SHA-256. The envelope binds
execution/replay times, present-freshness assessment without a current claim,
terminal R5 status, source fingerprint, sole-source/no-combination semantics,
and zero incremental spend/call counters. No audit or result is created.

## Two independent reviews and aggregate GO

Stage B stays locked until the two predeclared reviewers create the exact
protocol-declared immutable review files. Reviewer 1 is
`/root/final_transport_fix` (Goodall), authority
`independent-read-only-transport-and-release-auditor`. Reviewer 2 is
`/root/aggregation_regression` (Mendel), authority
`independent-read-only-fairness-budget-and-protocol-auditor`. They cannot be
swapped or replaced. Independence is process-attested, not cryptographic human
authentication. Each file must be canonical JSON with exactly:

```json
{
  "created_at_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "credential_recorded": false,
  "decision": "GO",
  "model_content_recorded": false,
  "no_score_read": true,
  "recovery_precommit_sha256": "<published recovery seal>",
  "review_authority": "<exact predeclared authority>",
  "reviewer_codename": "<Goodall or Mendel at its exact path>",
  "reviewer_id": "<exact predeclared canonical identity>",
  "schema_version": "narratordb.v13-paid-r5-recovery-go-review.v1",
  "score_blind": true,
  "source_attempt_tree_fingerprint_sha256": "db0f54b46b3c24c6fac212e59a472365451dd88638f2b8274bf94538b804f804",
  "stage_a_envelope_sha256": "<SHA-256 of fresh Stage-A envelope>",
  "terminal_failure_record_sha256": "5b2f8963ec0e8fa7fbbfb8df99692e49407d535ea80761952afc5c303c44e869"
}
```

Only after both reviews exist, an aggregate immutable GO may be created at the
declared fresh path. It repeats the seal, Stage-A envelope, source fingerprint,
and terminal hashes and contains `go: true`, `score_blind: true`,
`no_score_read: true`, false credential/model flags, plus a `reviews` array in
protocol path order. Each item has exactly `path`, `reviewer_id`, and file
`sha256`. Reviews must be created and physically written after Stage A; GO must
follow both. All three must be singly linked, `0444`, and under one hour old.

## Stage B

Stage B emits zero stdout and stderr bytes. It rechecks recovery seal, direct and
nested inputs, and the whole source fingerprint before and after score-bearing
computation. It builds eight release members in a private `0700` directory on the
output filesystem: checksum, result, two audits, exact copies of both reviews and
GO, and completion. Files are O_EXCL/fsynced/`0444`; the directory is
fsynced/`0555`; one `renameatx_np(RENAME_EXCL)` publishes the complete namespace.
No partial score-bearing release path can appear.

If a process ends after the exclusive rename, a later Stage-B invocation does
not trust a process-local flag. It strictly reconstructs and validates the full
immutable release, embedded reviews/GO, exact audits/result/checksum/completion,
Stage-A envelope, recovery seal, and source evidence. A valid release is already
committed success and can never gain a contradictory terminal record. A malformed
or partial release fails closed without writing such a record.

Any precommit nonzero destroys unread ephemeral score scratch, preserves all
pre-existing and published evidence, writes a generic immutable terminal status
when possible, and closes r2. Atomic rename is irrevocable; later housekeeping
cannot create a contradictory failure. Retrying requires a separately reviewed
and sealed r3; r1 is terminal and is never retried.
