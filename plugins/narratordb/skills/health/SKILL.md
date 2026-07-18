---
name: health
description: Diagnose NarratorDB database health and project-scope safety without changing stored data. Use when the user asks for status, health, readiness, scope verification, or troubleshooting.
---

# Check NarratorDB health

Perform one quiet, read-only health check:

1. Send one short progress update: `Checking NarratorDB health…`
2. Call `status` exactly once with `scope="project"` and `full_check=false`.
3. Interpret the structured response internally. Never echo its raw JSON.
4. Inspect these fields before choosing the result:
   - `health.ok`;
   - `scope_diagnostics.project_writes_blocked`;
   - `scope_diagnostics.warning`;
   - `memory_counts.current_workspace`;
   - `memory_counts.current_user_total`.
   - `capture_policy`.
5. Finish with one concise line:
   - blocked scope with healthy database: `NarratorDB's database is healthy, but project scope is unsafe: <brief warning>. Restart Codex from the intended project folder.`
   - healthy and not blocked: `NarratorDB is healthy — <mode> mode with <capture_policy> capture; <current_workspace> memory/memories in this project, <current_user_total> total for you.`
   - degraded database: `NarratorDB is degraded: <short reason>. <one recovery action>.`
   - unavailable MCP: `NarratorDB is unavailable. Verify the installation, then restart Codex.`

When `project_writes_blocked=true`, describe database health and project-scope
safety separately. Never call project memory healthy or ready while that block
is active. Use the returned warning only as a brief explanation; the recovery
instruction must tell the user to restart Codex from the intended project
folder. If the warning is empty, use `Codex started outside a confirmed project
folder` instead. If the database is also degraded, report both problems without
calling the database healthy.

When writes are not blocked but `scope_diagnostics.warning` is present, append
one short `Scope warning: <warning>.` clause to the healthy result. Do not list
database paths, user IDs, workspace IDs, health objects, timing fields, or
other counts unless the user asks for diagnostics.

Use correct singular/plural grammar for the two memory counts.

Do not call `recall`, `resume`, `remember`, `remember_session`, or `forget`.
Private mode needs no model-provider key. Never display environment contents or
secret values. Codex controls native status/spinner animation; this skill can
provide concise commentary but cannot customize that animation.
