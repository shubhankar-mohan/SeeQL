# SeeQL docs

Complete documentation for [SeeQL](../README.md) — the LLM-powered MySQL
DBA agent. Read the README first for a 5-minute intro; this directory is
the reference.

## Getting started

- [Install & deployment](deployment.md) — Docker, compose, Kubernetes sketch,
  GCP Cloud SQL walkthrough
- [Configuration](config.md) — every `settings.yaml` key and environment
  variable, grouped by subsystem
- [CLI](cli.md) — every `seeql` subcommand with flags, exit codes, examples
- [Troubleshooting](troubleshooting.md) — "my logs say X" answers + gotchas

## Architecture

- [System architecture](architecture.md) — data flow, design rationale,
  collector → SQLite → agent → alerts pipeline
- [Collectors](collectors.md) — what each of the 19 metric collectors
  reads, which tables it writes, which MySQL permission it needs
- [Agent](agent.md) — LLM agent layer, state builder, tools, provider
  matrix (Anthropic / Vertex / Gemini)
- [Alerting](alerting.md) — 6 deterministic rules + anomaly detection,
  cooldowns, channels (Slack, webhook, log)
- [Incidents & replay](incidents.md) — gap-based incident windowing,
  `seeql replay` timeline reconstruction, LLM root cause narration
- [Dashboard](dashboard.md) — per-page tour, HTMX auto-refresh, ARIA

## Reference

- [HTTP API](api.md) — every `/api/v1/*` and legacy `/data/*` endpoint
- [Error catalog](errors/) — E001–E010, indexed from `seeql/errors.py`

## Tutorial

- [First incident walkthrough](tutorial-first-incident.md) — from a cold
  start through a simulated lock cascade, end-to-end

## Contributing

- [Contributing guide](../CONTRIBUTING.md)
- [Changelog](../CHANGELOG.md)
