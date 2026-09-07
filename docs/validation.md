# Validation and release evidence

The release process validates behavior, packaging, and publication separately.
The GitHub release carries source and wheel digests plus a validation summary.
Checks can run locally without hosted CI or paid services.

## Behavior checks

The test suite includes:

- Real command capture, bounded reads, malformed UTF-8, exit status and process-group cancellation.
- Durable start/terminal records, concurrent journal writers, corruption, dead owners and bounded inventory.
- Local HTTP acceptance, partial rejection, redirects, timeouts, slow headers/bodies and replay integrity.
- Real Git worktree removal and fresh independent restoration of HEAD, staged changes,
  working bytes, modes, ignored files, unusual names, renames and deletions.
- Clean-looking files whose on-disk modes or line endings differ from Git's stored content.
- Active/malformed/new-owner leases, lock replacement, released/reused permits,
  concurrent consumers, active nested process directories and tampered archives.

No test is permitted to act on a real user's worktree or journal. Files, process
fixtures, ownership records and HTTP endpoints are temporary and synthetic.

## Packaging checks

```bash
uv sync --group dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
uv build
```

Install the built wheel into a fresh environment outside the checkout. Run the
README capture/read example, inspect the resulting operation, and exercise
archive/remove/restore on a disposable repository. Confirm that imports resolve
from the installed package and no private source tree or ambient `PYTHONPATH` is
required. Test the minimum supported Python version as well as a current version.

Before publication, inspect the source/wheel inventories, check licenses and
third-party notices, scan the actual release contents for secrets, and bind the
results to the exact source commit and artifact digests. Public history must not
include private journals, old repository history, production configuration or
internal review transcripts.

## What these checks do not prove

Passing tests do not prove that a user installed the tool, that a product adopted
it, that a collector policy is active, that remote backends retained every event,
or that model-token costs decreased. Those require their own environment- and
workflow-specific evidence. This alpha is not a production certification.
