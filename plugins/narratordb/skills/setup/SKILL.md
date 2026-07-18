---
name: setup
description: Configure NarratorDB Private or Intelligence mode and Manual, Preferences, or Sessions capture. Use when the user asks to set up, change, or explain NarratorDB mode, automatic learning, privacy, or model enrichment.
---

# Configure NarratorDB

Use `status` once to inspect the current choices. Interpret structured output
internally and never echo raw JSON, paths, identifiers, or environment values.

If the user has not chosen both axes, explain them briefly and ask for the
missing choice before changing anything:

- Private: storage and recall stay local; no compiler or provider call.
- Intelligence: storage and recall stay local, while captured sessions may be
  compiled by the explicitly selected local or hosted model.
- Manual: save only explicit remember requests.
- Preferences: also learn narrow, high-confidence personal preferences and
  routines from each current prompt.
- Sessions: Preferences behavior plus bounded project conversation capture.

Recommend `Private + Sessions` for a local, automatic first experience.
Recommend `Private + Preferences` when the user wants less retained project
conversation. Do not call Intelligence “cloud storage”; its database remains
local unless a separate remote NarratorDB service is configured.

Call `configure` exactly once after the choices are clear:

- Private: pass `mode="private"` and the selected `capture_policy`. If leaving
  Intelligence, default to `derived_data="retain"`; purge only on an explicit
  deletion request.
- Intelligence: pass `mode="intelligence"`, the selected `capture_policy`, and
  a compiler. Use `local` only with a loopback endpoint and model. Use `openai`,
  `openrouter`, or `codex-cli` only when the user explicitly selects it. Never
  request or pass an API key as a tool argument; credentials come from the MCP
  server environment.

Finish with one line:
`NarratorDB configured — <mode> mode, <policy> capture, local database and recall.`
For Intelligence, append `Write-time enrichment uses <compiler/model>.`

Codex owns the native tool and hook spinner. Use one concise progress update;
never simulate animation with delays or repeated messages.
