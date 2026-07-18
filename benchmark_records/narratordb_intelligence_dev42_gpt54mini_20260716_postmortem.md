# NarratorDB Intelligence V6 dev-42 postmortem

Status: inspected development analysis. This document does not amend the
immutable run, create a V7 score, or establish a Mem0 comparison.

## Frozen run

- Record: `narratordb_intelligence_dev42_gpt54mini_20260716.json`
- Artifact: `reports/longmemeval-intelligence-dev42-gpt54mini-20260715`
- Dataset questions: 42 declared development IDs, all retained
- Ingestion: 10,375/10,375 pairs, zero failures
- Compiler: GPT-5.4 Mini, Azure only, minimal reasoning, prompt V6
- Compiler jobs: 1,979/1,979 complete; every response ended with `stop`
- Evaluation: GLM 5.2 answerer and dated DeepSeek V4 Flash judge
- Cost: $24.408927 compiler + $0.491719 answer/judge = $24.900646
- Source state: the engine worktree was dirty; the record freezes the commit,
  tracked diff hash, source manifest, database hash, and compiler-cache hash

| System on the same restored IDs | Top 20 | Top 50 |
|---|---:|---:|
| Immutable NarratorDB 1.3 outputs, post-hoc slice | 37/42 (88.0952%) | 36/42 (85.7143%) |
| NarratorDB Intelligence, compiler V6 | 29/42 (69.0476%) | 31/42 (73.8095%) |

The V6 Intelligence run is five questions and 11.9048 percentage points below
the 1.3 same-ID diagnostic at top 50. The split was inspected and the 1.3 slice
was reconstructed after its ID archive was restored, so neither number is an
untouched holdout comparison.

## Failure localization

Every one of the 11 V6 top-50 misses was inspected. The category describes the
first failing stage, not a benchmark-specific rule to add.

| Question ID | Type | First failing stage | Observed mechanism |
|---|---|---|---|
| `1568498a` | single-session-assistant | extraction omission | V6 omitted the assistant's chess continuation `28. Kg3`; relevant raw evidence was below top 50. |
| `1b9b7252` | single-session-assistant | extraction omission | V6 did not preserve the recommended mindfulness resource, Mindful.org. |
| `41275add` | single-session-assistant | extraction omission | V6 omitted the exact Mayo Clinic recommendation and URL; raw evidence ranked 51. |
| `8752c811` | single-session-assistant | extraction omission | V6 did not preserve item 27 from a long numbered assistant list. |
| `09d032c9` | single-session-preference | retrieval/ranking | The previously purchased portable power bank was stored but absent from returned context. |
| `88432d0a` | multi-session | retrieval/ranking | The completed whole-wheat baguette evidence was stored but absent, while a planned chicken-wings item was returned and counted. |
| `9d25d4e0` | multi-session | retrieval/ranking | Necklace/ring acquisition evidence was absent while a locket distractor was returned. |
| `afdc33df` | single-session-preference | retrieval/ranking | Utensil-holder and granite-surface context existed but was missing from the returned candidates. |
| `0a995998` | multi-session | reader synthesis | All relevant clothing tasks ranked 3–16, but the reader conflated pickup and return states. |
| `37f165cf` | multi-session | reader synthesis | The two page counts, 440 and 416, were in the top five; the reader did not produce their sum, 856. |
| `618f13b2` | knowledge-update | contradiction resolution | Four-versus-six state survived under different keys/timestamps, and the wrong current count won. |

Under the simplifying assumption that each localized repair flips exactly its
own verdict and causes no regression, recovering the four extraction omissions
would produce a 35/42 subtotal; recovering those plus the four retrieval
failures would produce 39/42. These are stage-local counterfactual counts, not
measured scores or ceilings.

## Post-run V7 changes and hypotheses

1. Compiler V7 receives at most eight locally selected active prior claims as
   untrusted hints so explicit updates can reuse a stable memory key. Only the
   current session's canonical messages remain valid evidence.
2. The prompt now retains salient assistant recommendations, resources, plans,
   and commitments instead of treating assistant advice as categorically
   ephemeral.
3. Cache format v3 includes the exact prior-claim context in its identity.
4. Intelligence top-k uses weighted reciprocal-rank fusion of raw hybrid hits
   and claims, with a slight raw prior so derived claims cannot monopolize the
   cutoff.
5. Query-anchored, bounded session-neighbor expansion recovers a relevant
   assistant turn after a matched source row. Filtered searches disable this
   expansion so it cannot bypass provenance constraints.
6. Structural excerpts retain requested numbered items and URL-bearing lines.
7. Requested candidate count is independent of the rendered-context token
   budget.
8. Query-free explicit finalization moves compiler work to the ingestion
   lifecycle. Compatibility lazy finalization remains measured separately from
   local retrieval for the unchanged harness.

None of these changes contains benchmark IDs, expected answers, judge outputs,
or question-type branches. They are product-level hypotheses and remain
unscored until a fresh run is frozen.

## No-cost replay evidence

A retrieval-only replay used a copy of the frozen V6 database, made no network
or hosted/generative model calls, and did not modify the immutable artifact. A
locally cached SentenceTransformer may still participate in retrieval. The new
fusion path returned 50 candidates and moved the five known V6-versus-1.3
regressions into the top ten; ranks below are 1-based:

| Evidence | New rank |
|---|---:|
| Chess setup / raw `28. Kg3` | 7 / 8 |
| Mindful.org | 5 |
| Mayo URL | 8 |
| Numbered item 27 | 2 |
| Kitchen holder/granite context | 7 |

This verifies retrieval mechanics only. It is not an answer/judge score and
does not prove that other questions cannot regress. These ranks are a
working-tree diagnostic rather than a frozen replay artifact. Before
publication, archive a replay JSON with the source database hash, frozen source
snapshot hash, exact queries and retrieval configuration, 1-based ranks, and a
checksum.

## Next controlled experiments

1. Pass the complete deterministic release suite, inspect all diffs, and freeze
   the exact source snapshot.
2. Run the isolated three-call
   [synthetic compiler canary](../BENCHMARKS.md#synthetic-intelligence-compiler-canary)
   for V7 GPT-5.4 Mini on a fresh temporary database/cache/ledger.
3. If its built-in gates and declared-route review pass, run the separate
   six-question official-harness benchmark canary on a fresh benchmark state.
4. If both canaries pass, run all 42 development questions with GPT-5.4 Mini
   and archive the complete result regardless of score.
5. Repeat the three-call smoke, six-question benchmark canary, and full dev-42
   protocol with Luna Pro, using fresh state at each disclosed boundary.
6. Keep the GLM 5.2 answerer, dated DeepSeek judge, IDs, prompts, cutoffs,
   retries, and denominator fixed for the compiler-profile comparison.
7. Freeze the chosen architecture before evaluating a previously uninspected
   holdout or full official set. In a later reader/judge matrix, freeze each
   memory system's own retrieval payload once and reuse that system-specific
   payload across every reader/judge condition.

The shipped profiles use GPT-5.4 Mini with `minimal` reasoning and Luna Pro
with `low`. The resulting run compares two production profiles, not a
single-variable model substitution. A component-isolated model comparison
would require one reasoning effort supported by both models.

Paid benchmark processes require an explicit local soft USD stop plus an
external account-wide cap. The local stop is cumulative per usage ledger and
can be exceeded by an in-flight call; fresh ledgers do not create a shared
global budget, and the mechanism does not convert EUR. Allocate the combined
canaries, full runs, retries, answering, and judging below the external cap and
record every effective value. Production projects have no experiment-specific
package default; operators configure their own soft quota with
`NARRATORDB_COMPILER_MAX_COST_USD`.
