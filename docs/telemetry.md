# Optional telemetry

No export occurs unless `TOKEN_BURN_OTEL_ENABLED` is `1`, `true`, or `yes`.
The `OTEL_TRACES_EXPORTER=none` and `OTEL_LOGS_EXPORTER=none` disable flags win.
Journals and evidence are still retained when network export is disabled.

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `TOKEN_BURN_STATE_DIR` | `~/.local/state/token-burn/operations` | Private operation journal root |
| `TOKEN_BURN_OTEL_ENABLED` | Off | Explicit export opt-in |
| `TOKEN_BURN_OTLP_ENDPOINT` | `http://127.0.0.1:4318` | Loopback OTLP/HTTP base |

When the token-burn endpoint is unset, the resolver also recognizes, in order,
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_HTTP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_ENDPOINT`, and `OTEL_COLLECTOR_ENDPOINT`. An explicitly declared
port is used as given. It is never silently translated from gRPC to HTTP.

Only HTTP on literal loopback or `localhost` is admitted. `localhost` is pinned to
IPv4 loopback without a DNS lookup. Credentials, query strings, fragments, remote
hosts and arbitrary URL paths are refused. The local collector owns remote
backend routing and authentication. No authentication headers or proxy settings
are inherited by token-burn's HTTP client.

The [example receiver](../examples/otel-collector.yaml) uses the standard OTLP
HTTP receiver and a basic debug exporter. It is a starting configuration, not an
installed or production-ready monitoring service.

## Delivery semantics

- Each event retains immutable OTLP trace/log payloads and independent delivery state.
- Start and terminal boundaries attempt a bounded export when enabled.
- One monotonic deadline covers connecting, sending, headers and response body.
- Retryable failures remain pending. Permanent rejection or populated partial
  acceptance is held for inspection. Empty `partialSuccess: {}` is full acceptance.
- Replay is at least once. Stable span IDs support correlation; log consumers
  should deduplicate by `event_id`.
- `replay` never repeats the original command, archive, restore or removal.

Offline runs still contain pending payloads. Enabling export does not replay old
runs automatically. Choose which runs to replay and which inventory to monitor.
Pending payloads in an intentionally local-only workflow are not a network outage.

## Status and metrics

```bash
token-burn status RUN_ID
token-burn history RUN_ID --limit 20
token-burn replay RUN_ID --limit 20 --timeout 0.5 --max-seconds 3
token-burn snapshot --limit 100 --max-events 200 --max-seconds 2
token-burn metrics --limit 100 --max-events 200 --max-seconds 2
```

Inventory is bounded and reports incomplete or unreadable state explicitly.
The scan time budget is checked between filesystem reads; it cannot preempt a
blocked filesystem call. Metrics have fixed names and state labels, with no
run IDs, paths or process IDs in labels.

Use your existing textfile publisher or metrics collection process. The package
does not install a scheduler or HTTP server. The [example alert rules](../examples/prometheus-alerts.yml)
cover missing terminal receipts, unverified recovery, failed child reaping,
stalled/held telemetry, incomplete inventory and stale monitoring snapshots.
Use delivery alerts only for an inventory whose export is intentionally enabled.
Freshness alerts require an actual publisher running more often than five minutes.

## What proves delivery

Collector acceptance proves only that boundary. To verify backend storage:

1. Run a disposable success and an intentional failure.
2. Read each local event set and its trace ID.
3. Query the chosen backend over that run's time range.
4. Compare the complete event-ID set, root span and terminal outcome.
5. Retain those query results privately alongside the local receipts.

Do not infer storage from an HTTP health response, a collector acknowledgment,
or a green metrics scrape. Logs, traces and metrics can have different delivery
or retention behavior.

These deterministic operation events are operational evidence. Model generations
and AI evaluations belong in the application's existing AI-observability system.
Opaque trace/run references can correlate them without copying private content.
