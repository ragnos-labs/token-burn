# Integration contracts

## Capture and inspect

```python
from pathlib import Path
from token_burn.evidence import capture, read_page, summary, write_cases

receipt = capture(["python", "-c", "print('hello')"], Path("/private/evidence/run-001"))
print(summary(receipt, budget=6000))
page = read_page(Path("/private/evidence/run-001/stdout.log"), offset=0, limit=2000)
```

Run a command only after your application has authorized it. Capture inherits
the command's environment and permissions. Output directories must be new.
The JSON response and `receipt.json` expose the observed command exit status;
the operation sidecar separately describes terminal-history and export success.

`write_cases(records, directory)` accepts records with a nonempty `case` string
and a boolean `pass`. All records and failures are retained before the summary
is returned. Supplied pass/fail assertions remain the producer's responsibility.

## Operation records

```python
from token_burn.operations import Operation

operation = Operation.open("a_valid_32_hex_run_id")
status = operation.status()
events = operation.read(offset=0, limit=20)
```

The durable operation schema is `agent_operations.v1`; the inventory schema is
`agent_operations_snapshot.v1`. Operations correlate with `run_id`, `trace_id`,
`root_span_id`, and individual `event_id` values. Preserve outcome, evidence
completion, delivery status and backend verification as separate fields.

The public Python API is alpha. The versioned records are the preferred boundary
for external applications. Consumers must validate the schema version, retain
unknown/incomplete states, and avoid interpreting a missing field as success.

Raw local status can contain private evidence references. A dashboard adapter
should select only the fields it needs, such as operation, state, reason,
timestamps, inventory completeness, delivery counts and opaque IDs. Do not
forward arbitrary local records or private artifacts to a remote interface.

`source_revision`, when present, identifies the clean toolkit source checkout.
It does not identify the captured command's repository. Installed wheels can
leave it unset; use the package version and release digest for package identity.

## Ownership adapters

```python
from pathlib import Path
from token_burn.worktree.leases import Approval

class ApplicationAuthority:
    def inspect(self, worktree: Path) -> Approval:
        # Read your existing owner service. Fail closed if it is unavailable.
        record = read_current_owner(worktree)
        return Approval(
            lease_id=record.owner_epoch,
            generation=record.generation,
            released=record.explicitly_released,
        )
```

`read_current_owner` is application code in this illustrative adapter, not a
token-burn function. The adapter must return facts for the exact worktree being
requested. Owner IDs must change across owners; generations must reflect changes
within an owner. Missing, malformed, stale or unavailable authority must raise.

Pass the same trusted adapter to `create_archive`, `acquire_permit`, or
`remove_worktree`. The toolkit rereads its facts at admission and immediately
before removal. An adapter does not acquire distributed locks by itself; the
application remains responsible for excluding writers it controls.

## Product boundaries

| Owner | Responsibility |
|---|---|
| token-burn | Evidence retention, bounded reads, operation records, local recovery mechanics and checks |
| Calling application | Command/action authorization, repository scope, owner truth and writer exclusion |
| Evidence storage owner | Private custody, access, backup and retention policy |
| Observability operator | Collector installation, credentials, routing, retention and backend verification |
| Dashboard/controller | Bounded projections, presentation and operator decisions |

This repository has no dependency on Workspace, Controller, a fleet scheduler,
Treeage, an issue tracker or a secret manager. Workspace package adoption and a
Controller status adapter are possible follow-up migrations, not features that
this release silently installs.
