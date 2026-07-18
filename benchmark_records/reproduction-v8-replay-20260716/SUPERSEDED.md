# Superseded before execution

This preflight was created while the replay run root was still absent. Review
then found that the recorded `git -C vendor/memory-benchmarks archive --output`
path would be resolved relative to the harness repository rather than the
NarratorDB repository. No replay command was executed and no V8 run root was
created from this preflight.

The record is preserved as a failed planning artifact. Execution uses the
freshly generated `reproduction-v8-replay-20260716-attempt2` preflight, which
fixes the archive path and explicitly records the otherwise default merge,
rate, and seed flags.
