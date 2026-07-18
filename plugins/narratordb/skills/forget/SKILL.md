---
name: forget
description: Safely remove a specific NarratorDB memory when the user asks to forget or delete stored information. Use confirmation and avoid broad deletion by default.
---

# Forget from NarratorDB

Deletion is destructive. Follow this sequence:

1. Use `recall` to identify the smallest matching set of memories. Show concise, non-secret descriptions and stable identifiers when the tool returns them.
2. If the target is ambiguous or the request could remove multiple memories, ask the user to choose. Do not infer permission for project-wide or user-wide deletion.
3. Obtain explicit confirmation immediately before deleting, unless the user already named an exact memory identifier and unambiguously requested its deletion in the current message.
4. Call `forget` using the schema exposed by the installed MCP tool and only the confirmed identifiers or scope.
5. Report what was deleted and what, if anything, was not found. Never edit the SQLite database directly and never simulate success when the tool is unavailable.

Requests to delete all memories require a clear scope and a fresh confirmation that states the scope.
