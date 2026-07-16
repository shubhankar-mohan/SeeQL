# SeeQL Demo — Grand Line dataset & static site

A One Piece ("Grand Line") themed dataset that lights up every SeeQL dashboard
view with a believable DBA story: a nightly **bounty-recalculation batch**
(`UPDATE bounties … JOIN pirates`) takes row locks on the hot `pirates` table,
blocking live crew writes, spiking `Threads_running`, and cascading toward
`max_connections` — exactly the lock-cascade → regression → incident →
investigation arc SeeQL exists to catch.

**No real database is touched.** The dashboard reads a pre-seeded SQLite file
(`data/grandline_demo.db`); no MySQL, no collectors, no GCP, no LLM calls.

Theme schema: `pirates`, `crews`, `bounties`, `devil_fruits`, `islands`,
`log_poses`, `marine_reports` (DB `grandline`, server `grandline-prod`).

---

## Prerequisites

The dashboard is a FastAPI app and needs the web extras (`fastapi`, `uvicorn`,
`jinja2`). Either use the repo's `./venv` (already has them) or install them:

```bash
pip install 'seeql[api]'          # into whatever env runs seeql
```

All commands below use `./venv/bin/python`; substitute your own Python if it has
the web extras.

## 1. Local demo dashboard (for screenshots)

```bash
# Build the themed SQLite DB (idempotent — rebuilds from scratch each run):
./venv/bin/python scripts/seed_demo.py

# Serve it read-only. NOTE: SEEQL_CONFIG selects the demo config; the dashboard
# never connects to MySQL. Port defaults to 8899 here.
SEEQL_CONFIG=config/settings.demo.yaml SEEQL_API_PORT=8899 \
    ./venv/bin/python -m uvicorn api.app:app --host 127.0.0.1 --port 8899
# open http://127.0.0.1:8899/dashboard
```

If your `seeql` CLI has the web extras installed, this also works (note that
`--config` is a **global** flag and must come *before* the subcommand):

```bash
seeql --config config/settings.demo.yaml serve --no-scheduler
```

### One-shot verification that every page is populated

```bash
bash scripts/verify_demo.sh      # boots, checks all pages + charts, tears down
```

Expect `DEMO VERIFY: OK`. Pages to screenshot:
`/dashboard` (overview), `/dashboard/queries`, `/dashboard/locks`,
`/dashboard/schema`, `/dashboard/server`, `/dashboard/todo`.

## 2. Static site for Vercel (free tier)

The static site is a frozen copy of the running dashboard — pure files, zero
runtime, safe on Vercel's free tier.

```bash
# With the demo dashboard from step 1 still running on :8899, in another shell:
./venv/bin/python scripts/export_static.py --base http://127.0.0.1:8899 --out dist
```

This writes `dist/` (pages as `<view>/index.html`, HTMX partials at their fetch
paths, chart/list JSON as `.json`, and `static/` assets).

### Preview locally (with the Vercel rewrites applied)

Plain `python -m http.server` will 404 the chart JSON because the app fetches
clean paths like `/api/v1/metrics/qps?range=1h` that `vercel.json` rewrites to
`…/qps.json`. Use the bundled preview server, which applies the same rewrites:

```bash
./venv/bin/python scripts/preview_static.py --dir dist --port 8001
# open http://127.0.0.1:8001/
```

### Deploy

```bash
vercel deploy --prod     # from repo root; vercel.json sets outputDirectory: dist
```

`vercel.json` handles clean URLs, JSON content-types, and the query-string-
agnostic API rewrites. **Known limitation:** only the default time-range each
page loads is captured, so non-default range toggles reuse that data — fine for
a demo.

## Regenerating

`data/grandline_demo.db` and `dist/` are gitignored build artifacts. Re-run
`seed_demo.py` then `export_static.py` to rebuild both from scratch.
