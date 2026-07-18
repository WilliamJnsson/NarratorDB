# NarratorDB Intelligence V7/V11 paired development postmortem

Status: complete, development-only, V11 not promoted. This analysis preserves
the measured result; it does not rescore, selectively rerun, or relabel V11.

## Result and integrity

The paired protocol evaluated byte-identical frozen prediction copies with the
same official harness, 42 inspected development questions, GLM-5.2 answerer,
dated DeepSeek V4 Flash judge, high reasoning, provider policy, and top-20 and
top-50 cutoffs.

| Variant | Top 20 | Top 50 |
|---|---:|---:|
| Contemporaneous frozen V7 control | 35/42 (83.3333%) | 38/42 (90.4762%) |
| V11 local replay over V7 compiler artifacts | 35/42 (83.3333%) | 37/42 (88.0952%) |

The V11 point estimate is one question and 2.381 percentage points lower at
top 50, so V11 is not promoted. Both 42-question evaluations are complete:
there are no dropped questions, final empty answers or judges, frozen-payload
mismatches, invalid route identities, unknown-cost attempts, selective
retries, or score-dependent reruns. The paired evaluation cost $1.352669714.

V11 prediction used no compiler, compiler cache, or hosted compiler call. It
replayed local retrieval and rendering against a copied, frozen V7 database.
That makes it a controlled consumed-development diagnostic, not a fresh V11
end-to-end score, untouched holdout, third-party reproduction, Mem0 comparison,
or publishable 95% result.

## Exact flip audit

Thirty-three of 42 questions had byte-identical full 200-result retrieval
payloads. Eight changed because V11 inserted an aggregation evidence pack; one
changed only below rank 131.

| Cutoff | PASS to FAIL | FAIL to PASS | Retrieval interpretation |
|---|---|---|---|
| Top 20 | `75f70248` | `afdc33df` | Both contexts were byte-identical; the flips are answerer/judge variance. |
| Top 50 | `09d032c9`, `618f13b2` | `9d25d4e0` | `09d032c9` was byte-identical variance; the other two were aggregation changes. |

The eight aggregation-pack questions scored 5/8 at top 20 and 6/8 at top 50
under both variants. Aggregation therefore had a neutral measured subtotal: it
created one useful cross-session gain and one cumulative-state loss.

- The gain, `9d25d4e0`, promoted engagement-ring evidence that had been just
  outside V7 top 50. Together with the necklace and earrings evidence, this
  enabled the correct count of three.
- The loss, `618f13b2`, packed both four-times and six-times observations plus
  an unrelated gift-budget fact. It compressed separately ranked six-times
  raw/claim support while repeated four-times evidence remained around the
  pack. Top 20 still answered six, but the stochastic top-50 answer chose four.
- The net aggregate loss, `09d032c9`, cannot be attributed to NarratorDB code:
  V7 and V11 supplied byte-identical results at all 200 ranks. V7 used sparse
  power-bank evidence and answered; V11 abstained.

The earlier frozen V7 evaluation scored 36/42 at top 20 and 38/42 at top 50,
while this contemporaneous evaluation of the same V7 predictions scored 35/42
and 38/42. Per-question judgments also swapped while the aggregate top-50 score
stayed constant. Hosted answerer and judge variance is therefore material even
at temperature zero and must be modeled rather than mistaken for retrieval
changes.

## Generic V12 correction

V12 must not contain benchmark IDs, expected answers, dataset terms, question
types, or judge-specific logic. The evidence supports four product-level
changes:

1. Distinguish distinct-event aggregation from latest cumulative snapshots.
   A query asking for a stated evolving count should retain explicit current
   evidence instead of flattening conflicting snapshots into an event list.
2. Rank and conservatively filter pack claims by query overlap across claim
   text, structured subject/predicate/object fields, and semantic memory keys.
   An unrelated numeric co-fact must not lead the pack merely because its raw
   source was densely retrieved.
3. Prioritize claim-FTS-linked sources before dense/raw overfetch while keeping
   a safe fallback for semantically related evidence with little lexical
   overlap.
4. Preserve independent events, complementary quantities, provenance, and
   explicit raw support. NarratorDB may organize evidence but must not silently
   compute the benchmark answer.

The correction is first covered with answer-independent synthetic tests, then
the full regression/package/stress suite. Only after the source is frozen may
the already-consumed dev-42 database be replayed locally. A paid evaluation is
admissible only if its complete protocol is committed before scoring. The final
95% claim remains reserved for a fresh precommitted or independent run.

## Bound artifacts

- Paired protocol SHA-256:
  `2ca59bd60ff0229f23d07845ad0e1afbd32150c713989ffae2e54501db18aa8f`
- V11 prediction manifest SHA-256:
  `1c5f074fbf0d11caa12650e97e7de27d878f43bcc4296cc0be1a7edede956783`
- V11 prediction audit SHA-256:
  `f2f44e87c39c6f7e9bdc79d35e3e1f1f37d04270eab2eca1be1d61ecc73ce59b`
- V7 evaluation audit SHA-256:
  `4250a93e60060b087e50ba17fe56249f64cb99df23fb0375b9dcc2b2f7639572`
- V11 evaluation audit SHA-256:
  `1c050c9ca7d1d1bb5a332b8e94e525cb425fbd97f561d2933872cef2244a472e`
- Paired result SHA-256:
  `2c1534c2a5d7786240d9af8ff1c2827311c827e723ba2fe4ff11fdc89f0222f3`
- Local artifact root:
  `reports/longmemeval-intelligence-dev42-v11-replay-v7gpt54mini-20260716`
