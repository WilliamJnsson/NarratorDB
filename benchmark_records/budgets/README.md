# NarratorDB historical 95% development-campaign budget

`narratordb_95_campaign_20260716.json` is a content-free declaration for
hosted-model spend in the now-closed OpenRouter-era development campaign. It is
a historical accounting boundary, not an active allowance and not authorization
for a first-party OpenAI run, an original-author benchmark suite, or any new
paid phase. Those require a separate predeclared budget. This campaign boundary
is the first GPT-5.4 Mini V4 hardening attempt at
`2026-07-15T14:48:57.665827+00:00`. It includes every discovered usage ledger
from that attempt through the paired V7-control/V11 dev42 evaluation, plus the
preserved synthetic canary records.

The V6 dev42 compiler and evaluator costs are represented once by the immutable
benchmark record (`24.90064606428` USD). Their two underlying usage ledgers are
therefore deliberately not listed. The V7 six-question gate is represented by
its compiler and evaluator ledgers. The V7 dev42 compiler phase is represented
once by its immutable compiler audit: 1,983 ledger events cost
`33.14190525` USD. The sealed ledger itself is bound into `V7_INPUT_SHA256SUMS`
and is not also listed, which prevents double counting. The V7 and V8 replay
evaluator ledgers are separate direct sources: 176 events cost
`0.52975236451` USD for V7 and 174 events cost `0.46747280219` USD for V8.
Their combined evaluation spend is `0.99722516670` USD.

The declaration also preserves the failed first GLM-5.2/DeepInfra V8 synthetic
canary as a zero-cost immutable source. It emitted no usage events and spent
`0` USD; retaining it prevents a failed transport attempt from disappearing
merely because no provider charge was recorded. The original compiler profile
hash is `5996e8d2664a96d22fb1373fc1e92092348fa9eebdd0a322c633a57ee831f65f`.
The generic transport repair was separately precommitted before retry in
`benchmark_records/profiles/glm52_deepinfra_transport_repair_precommit_20260716.json`.

The failed V10 fixed-FP4 synthetic canary is also preserved as an immutable
source. Its three compiler calls cost `0.01986928` USD and passed every memory,
grounding, exact-model, exact-provider, database, cache, and query-free recall
check. The overall canary remained failed because the successful direct route
did not include optional router-attempt metadata; no benchmark questions ran.

V11 repaired that generic false-negative without changing the compiler prompt,
memory schema, retrieval, route, or scoring behavior. Its frozen three-session
canary passed all 16 checks: 3/3 jobs completed on exact model `z-ai/glm-5.2`
and allowlisted final provider `parasail/fp4`, with zero recall-time model calls
and zero unknown costs. Optional attempt history was unavailable and explicitly
disclosed. The canary cost `0.01658584` USD and contained no benchmark question.
The provider's post-canary usage delta matched that amount exactly.

The later precommitted V7-control/V11 paired evaluation is also included. The
V7 control used 183 evaluator events and cost `0.734585144` USD; V11 used 181
events and cost `0.61808457` USD. Both ledgers are complete, have zero unknown
costs or invalid identities, and match the provider telemetry delta exactly.
Their combined cost is `1.352669714` USD. V11 scored 37/42 at top 50 versus the
control's 38/42 and was not promoted; preserving that outcome prevents a
negative experiment from disappearing from either the technical or spend
record.

The declaration also lists the paid V4 hardening attempt and its four diagnostic
canaries, the aborted V4 validation run, and the four aborted V5 runs. Their
closed subtotal is `42.90321480` USD. In the audit schema,
`active_usage_ledgers` means ledgers read directly instead of immutable summary
records; it does not imply that every listed ledger is still running.

After the preserved GLM route attempts, V10/V11 canaries, and paired evaluation,
the declared campaign total is `108.31010877191` USD, leaving
`141.68989122809` USD under the declared $250 campaign cap. Content-free
provider telemetry independently reported `128.637473952` USD of account
headroom; admission uses the smaller available headroom. Declaration schema v2 binds every compiler ledger to its
allowed request models and providers, and every evaluator ledger to its allowed
request models, response models, and providers. The auditor requires the full
producer event schema and closed metadata; the older v1 format remains readable
but is explicitly reported as syntax-only rather than closed-world.

New producer ledgers mark attempts whose upstream billing is unavailable as
`unknown_cost`. Such an attempt consumes its positive request reservation in
the local fuse, but the conservative placeholder is not presented as exact
provider spend: the campaign audit remains incomplete until it is reconciled.
Likewise, a successful completion with an unknown or mismatched model/provider
identity prevents publication even when it cannot contribute content through
the fail-closed proxy.

Rerunning the command below refreshes and verifies the total without mutating
this declaration. The checked output is stored in
`narratordb_95_campaign_20260716.audit.json`; refresh that derived snapshot
after rerunning the auditor, not the declaration. Any later paid phase is new
spend and requires an intentional declaration update.

Older full500 V4 Flash and GLM-5.2 comparison runs, the GLM preflight, Mem0's
published record, and non-hosted local work are outside this declared boundary.
They predate the current GPT-to-V7 development campaign and are not silently
treated as zero-cost campaign work. Provider dashboard spend may consequently
be greater than the declared campaign total.

Audit from the repository root with:

```console
narratordb-benchmark-budget-audit \
  --declaration benchmark_records/budgets/narratordb_95_campaign_20260716.json \
  --provider-cap-usd 250
```

The provider cap and observed hosted-model spend are both USD and can be
compared directly. The user's governance ceiling is 300 EUR. No FX source,
timestamp, or conversion policy was precommitted, so the auditor records that
ceiling but does not invent a conversion or claim EUR compliance. A later
cross-currency governance claim requires a separately declared, reproducible FX
policy.

## V13 paid-pair reservation

`narratordb_95_campaign_v13_paid_pair_20260716.json` is the intentional
declaration for the proposed V7-control/V13-first paid pair. It does not mutate
or replace the through-V11 declaration above. To avoid counting the old sources
twice, it binds the complete, publication-ready through-V11 audit as one
immutable baseline record at exactly `108.31010877191` USD, then declares only
the two new evaluator-ledger paths.

Both future ledgers are part of the closed declaration before execution. Their
request-model set is the exact GLM-5.2 answerer plus dated DeepSeek V4 Flash
judge IDs. GLM responses must use the same GLM ID. The judge response may use
only the exact dated ID or its exact canonical undated ID. Completion providers
must remain in the fixed five-provider allowlist.

The declaration is not an assertion that either future ledger already exists.
At the static precommit boundary, `attempt1` and its dynamic campaign audits are
deliberately absent and model/provider call counts remain zero. Dynamic
admission must create the two distinct predeclared blank-only ledgers, verify
their exact initial hashes and zero events, and then use the frozen V11 budget
auditor to create immutable phase-specific audits:

- `attempt1/precall/campaign-audit-before-v7.json`
- `attempt1/between/campaign-audit-before-v13.json`

Every mutable path above is rooted under the replacement campaign directory
`reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r2-20260716`.
The predecessor report root is reused only for its sealed, read-only staged
prediction inputs; no predecessor `attempt1` path is reused.

Neither audit may be overwritten. The first must pass before V7 starts; the
second must include the preserved V7 ledger state and pass before V13 starts.
Missing, unknown-cost, invalid-identity, over-cap, or arithmetically inconsistent
state fails closed. The separate R2 paid-pair protocol freezes the provider and
ECB FX admission rules; this budget declaration contains no credentials,
provider telemetry, FX observation, or model content.

After V13 completes, score release also requires a third immutable campaign
audit under `attempt1/postrun`, sanitized post-V13 provider telemetry, and fresh
ECB evidence. The provider usage increase from the pre-V7 observation through
the post-V13 observation must equal the exact sum of both evaluator ledgers
within `0.000000001` USD. The final campaign and provider caps and the buffered,
upward-cent-rounded EUR ceiling are rechecked with no unspent fuse. No score may
be read or compared until that reconciliation and both result audits pass.
