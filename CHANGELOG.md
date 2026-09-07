# Changelog

## 0.0.12

- Preserve native context-window model identifiers such as `claude-opus-5[1m]`
  through explicit collection, cache reuse, and outcome comparison.
- Reject malformed explicit native model identities as unavailable. Keep all
  non-model label contracts unchanged.
- Advance the usage parser to version 2. Recollect raw sources before comparing
  older reports; previous parser caches are invalidated automatically.
- Keep the 0.0.11 coding profile document and working behavior unchanged. Native
  paired savings remain unqualified; this is a parser compatibility correction.

## 0.0.11

- Explicit native Codex/Claude usage sources, bounded private cache, whole-task
  attribution, and complete-roster comparison across counterbalanced rounds.
- Separate cache/token components, incomplete-coverage states, and repository-qualified
  producer-supplied workflow acceptance; no assumed token or cost savings.
- Bounded protected handoff packets, a legacy envelope sealing primitive, and a
  packaged coding-efficiency profile with an exact digest.
- Additive CLI/Python interfaces, documented schemas, synthetic examples and tests.

Existing 0.0.10 commands, operation schemas, recovery guards and telemetry defaults
are preserved. This release does not activate integrations or certify native savings.

## 0.0.10

- Standalone command and case evidence capture with bounded JSON summaries and byte reads.
- Durable private operation journals, cancellation receipts, integrity checks and explicit replay.
- Opt-in loopback OTLP export and bounded status/Prometheus inventory commands.
- Local linked-worktree ownership, self-contained recovery archives, fresh-repository restoration
  and single-use guarded removal that retains branches and archives.
- Python interfaces, readable README diagrams, local examples, synthetic behavioral tests
  and an Apache-2.0 license.

This initial release does not migrate existing consumers or install production services.
