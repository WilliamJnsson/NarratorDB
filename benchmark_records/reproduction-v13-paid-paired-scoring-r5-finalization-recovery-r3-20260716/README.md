# R5 offline finalization recovery R3 — UNSEALED CANDIDATE

R3 is a new, score-blind, offline recovery namespace. It does not repair,
resume, relabel, overwrite, or retry the original terminal R5 attempt, terminal
R1, or terminal R2. It uses only the preserved paid-arm evidence from the
original R5 `attempt1`; it makes no model, judge, provider, FX, credential, or
network call and authorizes no incremental spend.

Do not execute `stage-a` or `stage-b` while this directory is unsealed. Before
sealing, only `preflight-smoke`, `stage-b-canary`, the zero-seal Stage-A launcher
canary, and the test suite are authorized. R3 has exactly 11 physical files
before sealing and must have exactly 12 after the independently reviewed
`SHA256SUMS` is added. The physical-file SHA-256 of that immutable seal must be
published externally before Stage A. No R3 Stage A, review, aggregate GO,
output, scratch, terminal status, release, or score publication exists in the
unsealed candidate.

## Why R3 exists: terminal R2

R3 directly binds all of the following immutable R2 evidence:

- external terminal record:
  `benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r2-7c18-terminal-20260716.json`,
  SHA-256
  `41f9a0bb9f7c7a5a1b9ff99565bc7b85b60cddf2eb948fcf0caefa3a94072235`
- terminal-record checksum manifest: SHA-256
  `496d592edb921779507b8b24d885ed639c00911ea30cd040f22fa1971d334722`
- sealed R2 bundle manifest: SHA-256
  `7c186797b14b297bd4035dff81e1b49bbc830cb776896bae90a3275751e116d9`
- R2 Stage-A envelope: SHA-256
  `daedd300de0ef2d86a41df8996326cb8cabd4b45c3be1e8d2ef5b4bc27d9463f`
- R2 terminal status: SHA-256
  `8cc3f2a46a06010775f7986cad021302af83f3c9ed5308622d9e936a62d1ec16`
- R2 Goodall and Mendel reviews: SHA-256
  `e594cd59dc2f2922cce01481eb33102dfe18e826eafd7a531f04e2f6d9367b77`
  and
  `25f7096a47640ad8a4d0340214c3b6e47f856cc284a5cbb4215348ec341b2428`
- R2 aggregate GO: SHA-256
  `bb10fc7b90e797fa7de9f1c3c039e8d6bb50ff1f17e7d81bda06a8b0e8fea4ff`

The exact authorized R2 Stage B started its worker and sandbox, emitted zero
stdout and stderr bytes, and exited 1 after `16.3988` seconds. The failure was a
deterministic V7 recovered-retry loader-policy mismatch: the generated V7 audit
correctly preserved `failed_attempt_counts: {"1": 1}`, was complete, official,
validation-clean, publication-ready, had no attempt-five failure, and was
recomputed byte-identically; the sealed R2 loader nevertheless required both
retry maps to be empty and rejected the legitimate recovered attempt-1 failure
as a terminal harness failure.

R2 stopped at V7 loader validation. V13 computation never started; no result or
completion was computed; no release, result, or score-bearing audit was
published. Private score-bearing scratch was destroyed. Neither the operator
nor the terminalizer read score-bearing values, and none were recorded in the
terminal evidence. The only R2 output files are the immutable hash-only Stage-A
envelope and generic terminal status. R2 is permanently terminal and cannot be
deleted, resumed, overwritten, or retried.

The R2 record also recursively preserves the terminal R1 provenance. R3 binds
the R1 terminal record, its checksum manifest, and the sealed R1 bundle as
direct bound inputs; neither R1 nor R2 is invoked again.

## Fresh R3 namespace and seal

R3 uses paths distinct from the original R5, R1, and R2 namespaces:

- bundle:
  `benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r3-20260716`
- output:
  `reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-finalization-recovery-r3-20260716`
- reviews: the two `.review-1.json` and `.review-2.json` siblings of the R3
  bundle
- aggregate GO: the `.RECOVERY_GO.json` sibling of the R3 bundle
- seal:
  `benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r3-20260716/SHA256SUMS`
- public seal environment variable:
  `NARRATORDB_R5_RECOVERY_R3_PRECOMMIT_SHA256`

The public seal is consumed by the outer launcher and passed as a worker
argument; it is not placed in the sandbox environment. The worker receives only
`HOME`, `TMPDIR`, `LANG`, `LC_ALL`, and `PYTHONDONTWRITEBYTECODE`. R3 pins every
absolute launcher executable by path, SHA-256, file facts, and code-signing
policy in `launcher-executables-r3.json`. In particular, it uses the present
`/bin/realpath`, SHA-256
`22cd7804166170874dddd490e146936b6957dbbbb93d14eacd86b8d0de3e3989`,
with Apple identifier `com.apple.realpath`; it never invokes the absent
`/usr/bin/realpath`.

The deny-default macOS sandbox denies all network families, permits repository
inputs read-only, and permits writes only to private scratch and the fresh R3
output root. The sole Darwin query permission is `sysctl-read`, required by
Python `ctypes` during `os.uname()` import. Scratch directories are `0700` and
private files are `0600` until immutable publication.

## Authorized preseal checks

`run_offline_recovery.sh preflight-smoke` performs the complete clean launcher
bootstrap, validates the pinned host and executable facts, and reaches the exact
sandbox/worker boundary with deliberately invalid worker arguments. It must
exit 0 with zero stdout/stderr and leave no R3 output, scratch, or bytecode.

The zero-seal canary runs the real Stage-A launcher path with a public seal of 64
zeroes. It must reach the absent/mismatched physical-seal check and exit 71 with
zero stdout/stderr and no R3 output, scratch, or bytecode. This preserves the
regression check for R1's earlier `/usr/bin/realpath` exit 67.

`run_offline_recovery.sh stage-b-canary` is an actual clean-launcher,
deny-default-sandbox, exact-worker smoke, but uses only synthetic score-shaped
data and disposable `/private/tmp` paths. It requires neither a seal nor a
credential and must exit 0 with zero stdout/stderr. It executes these exact
steps in order:

1. `synthetic-recovered-retry`
2. `generated-audit-parser`
3. `committed-audit-parser`
4. `result`
5. `completion`
6. `postscore-bundle`
7. `postscore-bound-inputs`
8. `postscore-attempt`
9. `atomic-eight-file-rename`
10. `committed-reentry`
11. `private-cleanup`

The canary must not create or modify the actual R3 output, reviews, GO, terminal
status, seal, or any score-bearing artifact. Its synthetic release and private
files are destroyed before success. The preseal test suite contains 33 tests;
all 33 must pass together with the three launcher checks before sealing.

## Exact recovered-retry policy

R3 fixes only the deterministic R2 loader-policy error. Each of
`failed_attempt_counts` and `timed_out_attempt_counts` must be a map whose keys
are only the strings `"1"`, `"2"`, `"3"`, or `"4"`; every present count must be
a positive, non-boolean integer. Empty maps remain valid. Attempt `"5"`, any
other or non-string key, zero, negative, boolean, fractional, or non-integer
counts are rejected.

Recovered retry maps are accepted only when all of these conditions hold
simultaneously:

- `complete` is exactly `true`;
- `official_harness_score_complete` is exactly `true`;
- every validation list is empty;
- `usage.publication_ready` is exactly `true`;
- `attempt_five_failures` is integer `0`; and
- `returned_none_responses` is integer `0`.

The worker temporarily clears only the two retry maps when calling the sealed
historical loader, then restores the validated maps. It does not relax any
other completeness, official-harness, validation, usage, terminal-attempt, or
returned-none rule. For each arm, the regenerated canonical evaluation-audit
bytes must SHA-256-match the immutable `internal_evaluation_audit_sha256` in
that arm's sealed `arm-gate.json`; retry normalization cannot substitute or
alter the commit-bound audit payload.

## Score-free failure reporting

The R3 terminal-status `failure_phase` is closed to this exact score-free enum:

- `launcher-or-worker-preflight`
- `stage-a-publication`
- `stage-a-validation`
- `stage-b-atomic-publication`
- `stage-b-committed-reentry`
- `stage-b-post-computation-recheck`
- `stage-b-pre-go-validation`
- `stage-b-result-and-completion`
- `stage-b-v13-audit`
- `stage-b-v7-audit`

Terminal status is generic, records no exception text or model content, and is
recursively rejected if it contains accuracy, correct-count, numerator,
metric, verdict, answer, judge, by-question-type, raw score-release, or related
score-bearing fields. Stderr is redirected to a sink. Any actual R3 precommit
nonzero destroys unread private scratch, preserves all pre-existing and
published evidence, writes this generic immutable terminal status when
possible, permanently closes R3, and forbids R3 retry. A later attempt would
require a fresh namespace, independent review, and separately sealed R4.

## Stage A — only after external seal publication

From the exact repository root, export the externally published physical-file
SHA-256 of the R3 `SHA256SUMS` as
`NARRATORDB_R5_RECOVERY_R3_PRECOMMIT_SHA256`, then run:

```text
benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r3-20260716/run_offline_recovery.sh stage-a
```

Stage A emits zero stdout/stderr. It recursively validates the R3 seal and bound
evidence, rechecks the immutable original-attempt fingerprint, and reconstructs
historical finalization at the preserved review time. It publishes exactly one
hash-only, score-blind historical envelope. Reconstructed documents and the raw
verifier report remain in memory and are represented only by SHA-256. Stage A
creates no evaluation audit, result, completion, release, or score publication.

## Independent reviews and aggregate GO

Stage B remains locked until the two exact predeclared reviewers create fresh,
canonical, singly linked, immutable, score-blind GO reviews after Stage A:

- Goodall, reviewer `/root/final_transport_fix`, authority
  `independent-read-only-transport-and-release-auditor`
- Mendel, reviewer `/root/aggregation_regression`, authority
  `independent-read-only-fairness-budget-and-protocol-auditor`

Each review binds the R3 seal, fresh Stage-A envelope SHA-256, original terminal
record SHA-256, source-attempt fingerprint, and exact reviewer identity. Only
after both reviews may the aggregate immutable GO be created. It binds both
ordered review file hashes and the same seal, envelope, terminal record, and
source fingerprint. Reviews and GO assert `score_blind: true`,
`no_score_read: true`, false credential/model-content flags, mode `0444`, link
count 1, and protocol freshness. Reviewer independence is process-attested, not
cryptographic human authentication.

## Stage B and atomic publication

Stage B emits zero stdout/stderr. It revalidates the seal, all direct and nested
inputs, R2 terminal provenance, reviews/GO, and the original-attempt fingerprint
before private computation. It applies only the exact recovered-retry policy
above, enforces each sealed arm-gate internal audit hash, and rechecks the
bundle, bound inputs, and original attempt after score-bearing computation.

It builds eight release members in a private `0700` directory on the output
filesystem: checksum, result, two evaluation audits, exact copies of both
reviews and GO, and completion. Files are created exclusively, fsynced, and
made `0444`; the release directory is made `0555` and fsynced. One
`renameatx_np(RENAME_EXCL)` publishes the complete namespace. No partial
score-bearing release path can appear.

On reentry, a surviving release is never trusted from process-local state. The
worker strictly reconstructs and validates the complete immutable release,
embedded reviews/GO, audits, result, checksum, completion, Stage-A envelope,
seal, and source evidence. A valid namespace is irrevocably committed success;
a malformed namespace fails closed without a contradictory terminal record.
