# ADR 0001: Open-core product and cloud boundary

- Status: Accepted
- Date: 2026-07-18
- Owners: NarratorDB maintainers

## Context

NarratorDB serves two different users. Individuals need a trustworthy local
memory engine that is private, portable, and free to operate. Product teams
need a managed, multi-tenant memory service with authentication, isolation,
metering, recovery, and support. Hosting the local engine without adding those
operational capabilities is not a sufficient paid product.

The MIT license intentionally permits unrestricted local use, modification,
embedding, and self-hosting. Usage limits embedded in MIT source would not be
an enforceable product boundary because recipients can remove them. Cloud
plans therefore enforce entitlements on infrastructure controlled by
NarratorDB, while the production cloud control plane remains a separately
licensed product.

## Decision

NarratorDB adopts an open-core B2B model:

1. **NarratorDB Community** remains MIT-licensed, local-first, useful without a
   subscription, and unlimited for local memory operations.
2. **NarratorDB Cloud** is a separately packaged proprietary service. Its
   multi-tenant storage, organization and project control plane, billing,
   entitlements, audit features, managed deployment, and operations are not
   distributed as part of the MIT Community package.
3. The hosted trial is bounded by server-side credits, projects, storage,
   request rates, and retention. Local Community usage is not artificially
   capped.
4. The private beta uses scale-to-zero infrastructure with hard cost ceilings.
   The always-on high-availability AWS profile is retained for later use and
   is deployed only when revenue or a customer contract justifies it.
5. Model enrichment initially uses customer-provided model credentials or
   separately metered credits. Unbounded model cost is never bundled silently.

## Product boundary

Community includes the local SQLite engine, local MCP tools, import/export,
client integrations, and public tool contracts.

Cloud includes hosted PostgreSQL tenancy and row-level security, OAuth and API
key administration, organization and project membership, subscription
entitlements, usage accounting, hosted recovery, audit logs, dashboards,
service operations, and enterprise deployment automation.

Interfaces shared between the products must live in Community only when they
are useful independently and do not expose cloud credentials, billing logic,
or operational implementation.

## Initial commercial model

- Community: free local use.
- Cloud trial: time- or credit-limited prototype access.
- Developer, Growth, and Scale: recurring plans with prepaid usage and hard
  spend controls.
- Enterprise/BYOC: commercial contract, support, and separately licensed
  deployment artifacts.

Exact prices and quotas remain commercial configuration, not invariants in the
Community source tree.

## Consequences

- Free local adoption is the acquisition channel rather than a hosted-cost
  liability.
- Companies may still self-host Community or independently build production
  infrastructure. Cloud must win on total operating cost, reliability, and
  support rather than artificial lock-in.
- Cloud-specific source already developed on private branches must be
  extracted and verified before any corresponding Community branch is made
  public.
- Licensing and contributor ownership must receive qualified legal review
  before external distribution of the proprietary package.

## Release gates

Cloud cannot be called commercially ready until it has server-side billing
entitlements, quota enforcement, cost alarms, a tested first-request wake-up
path, automatic session resume for supported MCP clients, migration and
recovery drills, and documented behavior when a subscription expires.

The HA profile cannot be deployed by default until recurring revenue reaches a
documented threshold or a signed customer requirement pays for the additional
availability.
