# Contributing

Keep the toolkit portable, local by default, and easy to explain from the README.
New integrations should use explicit interfaces rather than importing a product's
runtime, credentials, scheduler or policy code.

```bash
uv sync --group dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
uv build
```

For a behavioral change, include a meaningful failure or negative control and
prove the corrected user outcome. Recovery changes need actual disposable Git
repositories and restoration checks. Concurrency changes need a competing caller.
Telemetry changes need private temporary journals and local HTTP fixtures.

Keep all fixtures synthetic. Tests must not use production endpoints, credentials,
real user worktrees, or the developer's existing journal directories. Do not start
background services or install schedulers as part of a test.

The system probes require Git and `ps` (the `procps` package on minimal Linux
images). macOS recovery probes also use the system `lsof` command.

Update the README when the high-level behavior changes, and the relevant contract
document when a boundary or limitation changes. A passed unit test is not evidence
of installation, deployment, backend storage, or model-token savings.

Open a pull request with the problem, resulting behavior, validation and relevant
limitations. Source and release validation can run locally; no paid hosted CI is
required. Changes to defaults, privacy or removal authority need explicit review.

Efficiency contributions should start from [the public contracts](docs/efficiency.md).
Add synthetic native-format fixtures and negative controls for missing usage,
counter resets, inherited history, duplicate events, source changes, and rejected
quality. Preserve the whole task roster and all attempts. Cache changes need a
competing collector and cold/warm equality; profile changes need a bounded exact
resource digest. Keep private account discovery, schedules, policy, and deployment
configuration in the adopter. A released package pin is the shared implementation;
do not maintain a separate internal copy of portable behavior.

Contributions are accepted under this repository's Apache-2.0 license. Only submit
material you have the right to contribute, and preserve required third-party notices.
