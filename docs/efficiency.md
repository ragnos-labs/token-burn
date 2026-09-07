# Local efficiency contracts

The additive 0.0.11 commands measure explicitly selected native usage, compare
registered tasks, create local handoffs, and show a released coding profile. They
do not discover accounts, start models, install profiles, change worker admission,
or grant action authority. Existing capture, operations, telemetry, and recovery
contracts remain unchanged. Runtime dependencies remain the Python standard library.

## Collect explicit sources

```bash
token-burn usage collect --sources sources.json --state-dir /private/usage-state --output usage.json
```

The output file must be new. Its parent must already exist. Reports are atomic,
mode 0600 JSON files. The command prints a small result with coverage, manifest
identity, and output digest; it never prints native message content or source
paths. Exit zero means a report was written, not that its coverage is complete.

Source manifests are private local inputs. This complete one-task example uses
synthetic identities and illustrative paths. Digests and revision must be replaced
with facts from the registered task. A manifest can include up to 128 tasks and
256 explicitly selected sources; no directory is scanned.

```json
{
  "schema_version": "token_burn.sources.v1",
  "variant": "baseline",
  "tasks": [{
    "task_id": "repair-round1",
    "repository_id": "example/project",
    "client": "codex",
    "model": "example-model",
    "reasoning_effort": "high",
    "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "prompt_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "validation_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "round": 1,
    "archetype": "noisy_diagnosis",
    "sources_complete": true
  }],
  "sources": [{
    "source_id": "attempt1",
    "task_id": "repair-round1",
    "path": "/private/native-attempt1.jsonl",
    "format": "codex_exec_jsonl",
    "kind": "root"
  }]
}
```

Task IDs identify matched tasks, including their round, across both variants.
Repository identity is a semantic label such as `owner/repository`, never a local
path. The same repository-local PR number can belong to different outcomes.
Other labels are bounded nonprivate identifiers. Client is `codex` or
`claude_code`. The source format must match the task client. Source kind is
`root`, `child`, or `continuation`; list every attempt and required validation
model call against the original task. `sources_complete` is the producer's
explicit assertion that this list accounts for the whole task, not a discovery
claim the toolkit independently verifies.

Supported format selectors:

| Format | Native observations | Accounting |
|---|---|---|
| `codex_exec_jsonl` | `thread.started`, `turn.completed.usage` from `codex exec --json` | Each completed turn adds its usage; unfinished or failed native turns are unavailable |
| `codex_rollout_jsonl` | `session_meta.id`, `event_msg` token counts, `turn_context.model` | Per-session cumulative high-water marks, explicit resets, and duplicate sample removal |
| `claude_print_json` | A single `--output-format json` result with usage, session ID, and modelUsage | One native result; errors, absent model usage, and zero-token results are unavailable |
| `claude_transcript_jsonl` | Assistant `message.usage` records with message IDs | Deduplicate identical message IDs; conflicting usage for one message is unavailable |

Each source may also supply `account_ref` (an opaque label, default `default`),
`start_line` (one-based, default 1), and `end_line` (inclusive). JSON result files
do not accept line ranges. Codex identity/model metadata before the selected
range is retained; usage outside that range is excluded. A range ending beyond
the file is incomplete. Supply a frozen source or collect after its native writer
finishes. A source changing during collection is unavailable.

For a fork, continuation, or any selected range starting after line 1 in a
cumulative Codex source, supply `initial_usage`
with native `input_tokens`, `cached_input_tokens`, `output_tokens`, and optional
`reasoning_output_tokens` / `cache_write_input_tokens` at the beginning of the
selected range. Inherited counts seed counters and do not count as new work.
The toolkit never guesses that the child's first observed sample was all
inherited. Missing or contradictory inherited baselines are unavailable. Native
session IDs remain separate for siblings. Copied history is deduplicated; genuine
retry and continuation usage is retained. Do not list an aggregate source and a
detail source covering the same native usage. Detected overlapping formats,
duplicate native result captures, and cross-task attribution conflicts are
unavailable rather than discounted.
Aggregate replay detection uses native session and normalized usage observations,
not file formatting or the caller's source label. If two same-session aggregate
captures have identical usage and no distinct native event identity, their
attribution is ambiguous; neither is silently discarded as a free retry.

## Measurement and cache semantics

The primary measure is **processed tokens per assigned task**:

- Codex: native `input_tokens + output_tokens`. Cached input and reasoning output
  are already included; do not add them twice.
- Claude: native `input_tokens + cache_creation_input_tokens +
  cache_read_input_tokens + output_tokens`. Native input excludes both cache
  components.

Normalized components are `input_tokens` (full input), `fresh_input_tokens`,
`cache_creation_tokens`, `cached_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, and `processed_tokens`. Optional native cache/reasoning
counters default to zero according to these format contracts. Counters must be
nonnegative integers bounded by 2^63-1; booleans, negative values, and impossible
cache/reasoning relationships are invalid. For cumulative sources each component
has an independent high-water mark; component deltas can reflect provider counter
reattribution, so processed tokens are always recomputed from full input and output.
Cache/reasoning decreases never reset full input/output counters. A decrease in
both primary counters starts a new counter segment; a decrease in only one is
ambiguous and makes coverage unavailable instead of replaying the session total.
These measures are not dollar costs or a universal provider-quota formula.

`token_burn.usage.v1` records include `parser_version=1`,
`metric_version=processed_tokens.v1`, variant, `manifest_sha256`, `roster_sha256`,
task observations, bounded source diagnostics, totals, coverage, and cache counts.
The manifest digest binds the complete explicit private input. The roster digest
binds sorted task metadata excluding `sources_complete`. Digests use sorted,
compact ASCII JSON plus one final LF. Each source also carries the SHA-256 of its
exact file bytes. No source path, prompt, response, native account, or raw native
session/message ID is copied into a report or cache.

Every task keeps its metadata, `usage`, `observed_usage`, `observed_models`,
`model_evidence`, duplicate count, and `coverage={status,reasons}`. `usage` and
top-level totals are null when coverage is unavailable. `observed_usage` is only
a possibly incomplete diagnostic subtotal. Missing usage is never a zero-cost
task. Model metadata is labeled `native` when available and otherwise `declared`;
command/settings evidence belongs to the caller. Native observed model sets must
match between paired reports. `complete_tasks` and `total_tasks` count tasks in
one variant, not native processes or individual attempts.

The foreground collector is the sole cache writer for an explicit `--state-dir`.
The directory must be owned by the caller with no group/other access. An exclusive
OS lock gives an immediate `collector_busy` error to competing writers. The lock
file stays in place; an exited process releases its lock without a stale-PID
override. Adapters read immutable reports, never write the cache independently.

Cache entries bind parser version, the exact source manifest, and current source
content digests. Warm reads hash files again but skip unchanged parsing. Corrupt
or incompatible cache entries are reparsed. Cache state is capped at 16 MiB;
eviction reduces future reuse, never the already computed report. Native inputs
are bounded to 64 MiB per source, 256 MiB per collection, 1 MiB per JSONL line,
100,000 rows per source, and 32 observed models per source. Exceeded limits produce
unavailable coverage. Parsing reads only the manifest's exact paths. No scheduler,
credential discovery, service, or background collector is installed.

## Compare every registered task

```bash
token-burn usage compare --baseline baseline.json --candidate candidate.json \
  --outcomes outcomes.json --output comparison.json
```

`token_burn.outcomes.v1` is a producer-supplied acceptance contract:

```json
{
  "schema_version": "token_burn.outcomes.v1",
  "required_groups": [{"client": "codex", "repository_id": "example/project"}],
  "required_rounds": [1, 2],
  "required_archetypes": ["noisy_diagnosis"],
  "tasks": [{
    "task_id": "repair-round1", "repository_id": "example/project",
    "baseline": {"accepted": true, "evidence_level": "accepted_workflow", "evidence_refs": ["evidence:baseline1"]},
    "candidate": {"accepted": true, "evidence_level": "accepted_workflow", "evidence_refs": ["evidence:candidate1"]}
  }, {
    "task_id": "repair-round2", "repository_id": "example/project",
    "baseline": {"accepted": true, "evidence_level": "accepted_workflow", "evidence_refs": ["evidence:baseline2"]},
    "candidate": {"accepted": true, "evidence_level": "accepted_workflow", "evidence_refs": ["evidence:candidate2"]}
  }]
}
```

Register the complete matrix and quality rubric before collection. There must be
exactly one matched task per required group/round/archetype combination. Include
at least two counterbalanced rounds. Both report rosters and the outcome roster
must match exactly. Freeze model, reasoning effort, revision, task prompt,
validation, and scope. Profile loading is the intervention; the task prompt digest
still identifies the unchanged task itself. Keep failed attempts and retries in
the task totals. A failed task cannot be dropped from the denominator.

Evidence levels are `accepted_workflow`, `integration`, `component`, or `unknown`.
Both arms need `accepted=true`, `accepted_workflow`, and nonempty opaque evidence
references. The caller must actually validate those assertions. A command exit,
merge, installed package, or agent claim alone is not accepted workflow proof.

`token_burn.comparison.v1` exposes parser/metric versions; input digests;
`status=accepted|not_accepted|unavailable`; boolean `accepted`;
`quality_status=passed|failed|unavailable`; coverage counts/reasons;
`matched_pairs`; groups; and `outcome_evidence=producer_supplied`.

Each group has client, repository_id, repeatable_savings, and rounds. Round rows
contain round, matched_pairs, baseline_tokens, candidate_tokens,
baseline_tokens_per_task, candidate_tokens_per_task, reduction_tokens,
reduction_ratio, quality_status, coverage_status, and
`status=improved|not_improved|unavailable`. Token totals include all attempts.
Reduction ratio is `(baseline-candidate)/baseline`, null for zero baseline.
Different clients' tokenizers are never pooled into one savings ratio.

Acceptance requires any strictly positive reduction in every group in every
registered round, complete coverage, and unchanged accepted quality. No arbitrary
ten-percent threshold applies. Failed quality remains `not_accepted`; incomplete
or mismatched evidence is `unavailable`. The 2-client by 2-repository pilot with
3 archetypes and 2 rounds has 24 matched pairs, meaning 48 runs. Both usage reports
contain 24 tasks; the comparison's complete_tasks and matched_pairs are both 24.
Acceptance is bounded to that registered matrix, not a universal savings claim.

## Python interfaces

```python
from token_burn.usage import collect, compare, normalize_codex_usage, normalize_claude_usage

usage_report = collect(manifest, state_dir=private_state_directory)
comparison = compare(baseline_report, candidate_report, outcome_contract)
```

The two normalization functions accept one native counter mapping and return the
normalized components above. They do not normalize an entire cumulative history,
find files, or apply budget policy. Invalid counters raise `ValueError`
(`token_burn._local.ContractError`). Owner adapters may map that result into their
legacy unavailable state while preserving their own schemas.

## Handoffs and the released profile

`handoff create --input FILE --output FILE` accepts `{state, correlation?}`.
State has exactly objective and next_action (nonempty strings), authority and
test_state (nonempty objects), stop_conditions/changed_files/receipts/blockers
(string arrays), and active_processes (array). Changed files must be relative
paths without parent traversal. Unknown or missing protected fields are rejected.

`token_burn.handoff.v1` preserves this state and optional correlation. Correlation
may contain `legacy` (opaque string keys/values) and `operation` (the unchanged
32-hex run_id/trace_id and 16-hex root_span_id). This separates legacy identifiers
from `agent_operations.v1`. `handoff validate FILE` checks the schema and exact
serialized byte metadata; it never executes the next action or verifies authority.

The preferred packet size is 4096 bytes and the hard maximum is 32768, including
the final LF. Size is computed from compact ASCII JSON. Packets never silently
discard protected fields. The preference flag is conservative at its one-byte
boolean-width boundary. These local packets can contain sensitive prose; they
are not a general redaction or data-loss-prevention service. Keep them private
and select explicitly allowed fields before any separate remote publication.

Python `handoff.create(state, correlation=None)` and `handoff.validate(packet)`
return independent JSON snapshots. `handoff.seal(packet, max_bytes=32768,
preferred_bytes=4096, inclusive_preferred=False, trailing_newline=True)` is a
smaller migration primitive. It retains an owner's complete envelope, computing
only bytes and preferred_target_exceeded. It validates JSON/size, not state,
privacy, or authority. Its hard limit never exceeds 32 KiB. A legacy adapter can
select an inclusive preference and exclude the LF without copying the size loop.

`token-burn profile show` reads the packaged Markdown without installing it.
`--json` and Python `profile.show()` return `token_burn.profile.v1` with
profile_id=coding-efficiency, profile_version/runtime_version=0.0.11, the exact
document, and document_sha256 over its UTF-8 bytes. The resource is at most 4 KiB.
It points to existing skill discovery and never creates another skill registry.

## Contributing and self-use

Run `examples/efficiency_demo.py` for a completely synthetic, offline demonstration
of both collection and comparison. Its accepted comparison is a fixture result,
not measured model savings. New parser contributions need synthetic native-format
fixtures, incomplete/error controls, and preservation of distinct billed attempts.
Outcome adapters must retain repository identity and the original task roster.

Portable fixes belong in this package. Internal adopters pin the released wheel
and digest, load this released profile, and keep only owner-specific mappings and
private configuration. Do not vendor a second implementation, import a private
runtime, add account discovery, or turn advisory measurements into admission rules.
Package release, installed import provenance, observed profile loading, exercised
runtime, and matched-workflow acceptance are separate pieces of evidence. Default
adoption remains held until the required native benchmark passes.
