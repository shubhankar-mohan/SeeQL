# Contributing to SeeQL

Thanks for your interest in SeeQL. This document covers what you need to know
to run the code locally, the expected style, and how to open a PR.

## Dev setup

SeeQL runs on Python 3.12+.

```bash
# Clone
git clone https://github.com/shubhankar-mohan/SeeQL.git && cd SeeQL

# Create a venv and install the project in editable mode with dev + api extras
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[dev,api]"

# Verify the console script is on PATH
seeql check --help

# Initialize a local monitoring DB
seeql init-db
```

## Running tests

SeeQL uses `pytest`. The full suite runs without any external services
(it uses a synthetic SQLite DB fixture + mocks — no real MySQL required).

```bash
# Full suite
pytest -q

# Single file
pytest tests/test_incidents.py -v

# Single test
pytest tests/test_replay.py::TestReplay::test_timeline_only_when_no_llm -v

# With coverage
pytest --cov=. --cov-report=term-missing
```

The expected baseline is **79 passed, 11 skipped**. The 11 skipped are stale
tests that target a pre-`ServerContext` collector API — see the skip reasons
in `tests/test_collectors.py` and `tests/test_integration.py`. Rewriting those
against the new interface is a standing follow-up.

## Linting

```bash
ruff check .
```

## Code style

- Python 3.12. Type hints where helpful but not obsessive.
- **No ORM.** We're writing time-series data, not modeling a domain.
- **All SQL in `queries.py` files.** Never inline SQL in collector code.
  Collector queries live in `collectors/queries.py`; agent queries in
  `agent/queries.py`. Dashboard API queries can be inline (they're in
  `api/dashboard_api.py`).
- **Config via YAML** with env var substitution for secrets.
- **Error handling:** each collector fails independently, logs, and
  continues. One broken collector should never stop another.

## Architecture overview

- `collectors/` — data collection, extends `BaseCollector`, runs in three
  scheduled loops via APScheduler.
- `storage/` — SQLite writer, schema, migrations, retention.
- `alerting/` — rules, channels, anomaly detection, incident windowing.
- `agent/` — structured state builder, LLM agent, tool definitions,
  prompts, incident replay.
- `api/` — FastAPI routes: Prometheus, dashboard routes, `/api/v1` JSON API.
- `templates/` — Jinja2 templates for the sketch-aesthetic dashboard.
- `parsers/` — text-blob parsers (`SHOW ENGINE INNODB STATUS`).
- `scheduler/runner.py` — APScheduler orchestration, graceful shutdown.
- `main.py` — CLI entry point (argparse subparsers).

For the full context read `CLAUDE.md`. For the execution plan read `PLAN.md`
and `IMPLEMENTATION.md`.

## Opening a PR

1. Branch from `main`.
2. Write a focused commit message — what and why.
3. Make sure tests pass (`pytest -q`) and ruff is clean (`ruff check .`).
4. If you're adding a user-visible feature or fix, update `CHANGELOG.md`
   under `[Unreleased]`.
5. Open the PR against `main`.

## Releasing (maintainer notes)

1. Bump `version` in `pyproject.toml`.
2. Move entries from `## [Unreleased]` into a new release section in
   `CHANGELOG.md`.
3. Commit, tag `vX.Y.Z`, push tag.
4. The `release.yml` GitHub Action will build the Docker image and publish
   to `seeql/seeql:latest` and `seeql/seeql:X.Y.Z`. Required secrets:
   `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.
