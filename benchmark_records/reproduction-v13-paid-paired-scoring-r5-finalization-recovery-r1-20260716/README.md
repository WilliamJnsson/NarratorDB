# R5 offline finalization recovery r1 — UNSEALED CANDIDATE

This candidate does not repair, resume, or relabel the terminal R5 attempt. It
uses the completed immutable paid-arm evidence in a separate offline-only
recovery protocol. The original attempt remains a terminal finalization failure.

Do not execute this directory while it is unsealed. The recovery output root
must remain absent until an independently reviewed `SHA256SUMS` has been created,
made immutable, and its physical-file SHA-256 has been published externally.

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

## Stage A

After sealing, export the public (non-secret) SHA-256 of the physical recovery
`SHA256SUMS` file as `NARRATORDB_R5_RECOVERY_PRECOMMIT_SHA256`. From the exact
repository root, run:

```text
benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r1-20260716/run_offline_recovery.sh stage-a
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
when possible, and closes r1. Atomic rename is irrevocable; later housekeeping
cannot create a contradictory failure. Retrying requires a separately sealed r2.
