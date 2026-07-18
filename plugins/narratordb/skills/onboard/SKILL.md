---
name: onboard
description: Verify NarratorDB memory mode, automatic capture policy, and project readiness. Use on first install, after an upgrade, or when the user asks whether NarratorDB is ready.
---

# Onboard NarratorDB

Perform one quiet, read-only readiness check:

1. Send one short progress update: `Checking NarratorDB for this project…`
2. Call `status` exactly once with `scope="project"` and `full_check=false`.
3. Interpret the structured response internally. Read the current-project count
   from `memory_counts.current_workspace` and the user's all-workspace count
   from `memory_counts.current_user_total`. Do not echo raw JSON.
   Read the automatic policy from `capture_policy`.
4. Inspect `scope_diagnostics.project_writes_blocked` before calling the project
   ready. If it is true, finish with one warning and stop:
   `NarratorDB is healthy, but project memory is not ready — Codex started outside a safe project scope. Start Codex from the intended project folder and run onboarding again. (<current> current-workspace memories; <total> across your workspaces.)`
5. Otherwise finish with one compact result:
   - ready in Private mode: `NarratorDB is ready — Private mode, <policy> capture, local recall, <current> current-workspace memories (<total> across your workspaces).`
   - ready in Intelligence mode: `NarratorDB is ready — Intelligence mode, <policy> capture, local recall, <current> current-workspace memories (<total> across your workspaces).`
   - path-derived but writable: append one short form of `scope_warning`; do not
     claim the scope is portable across machines.
   - degraded: `NarratorDB is degraded: <short reason>. <one recovery action>.`
   - unavailable: `NarratorDB is unavailable. Install or verify uvx, then restart Codex.`

Do not call `resume`, `recall`, or `remember` during routine onboarding. The
`SessionStart` hook handles normal context loading; explicit resume and write
requests are separate interactions. Do not list database paths, user IDs,
workspace IDs, health objects, or timing fields unless the user asks for
diagnostics. Never say that the database is empty merely because
`memory_counts.current_workspace` is zero; report current-workspace and
all-workspace counts separately.

Private mode needs no account, API key, or external model. An existing
Intelligence database remains Intelligence, but its recall is still local. Do
not install unrelated packages, change mode, request a provider key, or expose
environment contents. Codex controls native status/spinner animation; this
skill can provide concise commentary but cannot customize that animation.

Policy meanings: `manual` saves only explicit remember calls; `preferences`
also learns high-confidence personal preferences and routines from the current
prompt; `sessions` additionally captures bounded project conversations. Never
describe Preferences as arbitrary AI extraction. If the user wants to change
mode or policy, use the `setup` skill rather than mutating configuration during
a readiness check.
