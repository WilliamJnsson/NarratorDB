# NarratorDB Service for Codex

Lifecycle capture for an authenticated NarratorDB service. This plugin is used
with the service MCP bridge installed by `narratordb service install-codex`.

Unlike the local NarratorDB plugin, it has no `UserPromptSubmit` hook. It sends
only bounded, redacted project conversation windows at `PreCompact` and `Stop`,
and only when the authenticated service project uses Sessions capture.

`narratordb service quickstart` installs this plugin automatically. The
standalone `narratordb service install-codex --credentials-file PATH` command
does the same for an existing service or Compose deployment. If the conflicting
local-database plugin is installed, review it and pass `--replace-codex` to
replace it explicitly.

The installer writes a mode-`0600`, secret-free pointer under the NarratorDB
user directory. The hook reads that pointer, launches the same Python
environment as the registered MCP bridge, and reads the bearer token only from
the existing credentials file. Provider credentials are never inherited.
