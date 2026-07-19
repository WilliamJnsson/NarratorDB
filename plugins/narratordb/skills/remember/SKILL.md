---
name: remember
description: Save a durable fact, preference, decision, convention, or completed-session outcome in NarratorDB. Use when the user explicitly asks to remember something or clearly requests persistence across future sessions.
---

# Remember with NarratorDB

For a normal single-item request, use this exact interaction:

1. Build one self-contained statement that will make sense without the current
   transcript. Put attribution and category detail in `content`.
2. Send one short progress update: `Saving that to NarratorDB…`
3. Call `remember` exactly once with:
   - `content`: the durable statement;
   - `scope`: `project` unless the user explicitly requests a cross-project
     preference, then `global`;
   - `source`: `user` for a user-stated fact, preference, decision, convention,
     or explicit remember request.
4. Interpret the structured result internally and finish with one polished line:
   - stored: `Remembered for this project: <brief description>.`
   - duplicate: `Already remembered for this project: <brief description>.`
   - failure: `I couldn't save that to NarratorDB: <short actionable reason>.`

For `scope="global"`, say `Remembered globally` or `Already remembered
globally` instead of `for this project`.

`source` is a strict enum. Its only valid values are `user`, `assistant`, and
`memory`. Never pass `system`, `developer`, `tool`, prose such as `explicit
user-stated convention`, a category name, or an attribution label as `source`.
Source is non-authoritative attribution only. Use `assistant` only for a durable
outcome the assistant actually completed; otherwise default to `user` for this
skill.

Example for “Remember that this project uses pnpm”:

```json
{"content":"For this project, the user requires pnpm for package operations.","scope":"project","source":"user"}
```

Do not call `status` or `recall` before a normal write. Do not retry a failed
call with a guessed schema. Do not print or restate raw JSON, `message_id`,
`workspace_id`, `ingest_ms`, or other tool fields unless the user asks for
diagnostics. Codex controls its native tool spinner; do not simulate an
animation with repeated messages.

Keep these content rules:

- Preserve whether the user stated something or the assistant completed it.
- Include the project or component when needed to prevent ambiguity.
- Split unrelated facts into separate memories only when the request genuinely
  contains multiple durable items; use one call per item.
- Never store passwords, API keys, access tokens, private keys, authentication
  cookies, or raw secret-bearing files. If the request contains a secret, store
  only a safe statement such as “Deployment credentials are configured
  externally.”
- Never claim success unless the tool reports `stored=true` or
  `duplicate=true`.

For an explicitly requested completed-session summary, make one
`remember_session` call instead. Message roles must also use only `user`,
`assistant`, or `memory`. Keep goals, decisions, changed components,
verification results, unresolved issues, and next steps; exclude filler and
speculation. Use the same one-update, one-call, one-line-result pattern.
