# Dashboard

Server-rendered HTMX dashboard at `http://<host>:8080/`. No SPA build,
no JavaScript framework — templates live in
[`templates/`](../templates/) and refresh via `hx-get` polling.

## Pages

| Path | Purpose |
|------|---------|
| `/` | Overview — health bar, active alerts, recent incidents, top queries, live locks |
| `/queries` | Top queries + recent regressions, per-query detail pane |
| `/locks` | Current lock waits + historical contention patterns |
| `/schema` | DDL change feed + table sizes + index usage |
| `/server` | Threads, buffer pool, QPS, GCP infrastructure metrics |
| `/incidents` | Incident window list (pre-formatted for `seeql replay`) |

## Auto-refresh

Most panels poll every 30 seconds via HTMX's `hx-get` with
`hx-trigger="every 30s"`. Refresh targets use `aria-live="polite"` so
screen readers announce updates without interrupting.

Panels that auto-refresh:

- Health bar (top of every page)
- Active alerts
- Current locks
- Recent incidents timeline
- Live processlist

Panels that are click-to-refresh or on-load-only:

- Top queries (expensive query — refreshes on page nav)
- Query detail pane (shows when you click a digest)
- Schema change list (slow-loop-driven, refreshes every 30 min)

## Sketch aesthetic

The dashboard uses a hand-drawn / blueprint style — thick strokes,
muted palette, dashed borders on interactive elements. All styling
lives in
[`static/css/dashboard.css`](../static/css/dashboard.css) and is
scoped by page.

## Query detail

Clicking a query digest opens a right-pane detail view that shows:

- Full `digest_text` (parameterized pattern) **and** `query_sample_text`
  (a real example with actual parameter values)
- A "Copy EXPLAIN" button that generates a runnable `EXPLAIN <sample>`
- Daily avg-time trend chart (Chart.js)
- Recent EXPLAIN plans captured for this digest
- Tables touched + indexes used

The "sample vs pattern" label distinguishes the two — a previous bug
showed only the placeholder version (`SELECT … WHERE col = ?`), which
isn't EXPLAIN-runnable. Fixed in the Phase 3 release; see
[CHANGELOG.md](../CHANGELOG.md).

## Accessibility

Shipped so far:

- `aria-live="polite"` on incidents timeline
- Semantic HTML (`<main>`, `<nav>`, `<section>`)
- Alt text on Chart.js canvases via `aria-label`

Known gaps (see [TODOS.md — Dashboard a11y](../TODOS.md)):

- Skip-to-content link missing
- Info tooltips are hover-only (need `tabindex="0"` + `:focus` styling)
- Some touch targets below 44 × 44 px

## Keyboard shortcuts

(None yet. Candidates: `/` focus search, `g` + letter for page nav.)

## Extending

Templates follow a base + partials pattern:

- [`templates/base.html`](../templates/base.html) — layout, nav, header
- `templates/dashboard/<page>.html` — page-level template
- `templates/partials/<component>.html` — HTMX-refreshable fragments

To add a new page:

1. Create `templates/dashboard/yourpage.html` extending `base.html`.
2. Add a route in `api/dashboard_routes.py`.
3. Add API endpoints in `api/dashboard_api.py` for any JSON data the
   page needs.

## Related

- [API reference](api.md)
- [Frontend design doc](../FRONTEND_CLAUDE.md) — internal, not in
  public image
