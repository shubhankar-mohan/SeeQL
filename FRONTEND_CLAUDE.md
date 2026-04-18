# FRONTEND_CLAUDE.md — Dashboard Frontend Context

This file captures everything needed to continue frontend development on the SeeQL dashboard. Feed this to an LLM when working on UI/templates/styling.

---

## What the Dashboard Is

A server-rendered monitoring dashboard for the SeeQL MySQL DBA Agent. 5 pages of tables, charts, and live-updating widgets that show MySQL database health at a glance. No build step, no npm, no SPA — just Jinja2 templates with HTMX for partial updates and Chart.js for graphs.

The audience is a 10-person team with no dedicated DBA or frontend engineer. The dashboard must be immediately readable and useful without training.

---

## Tech Stack (No Build Step)

| Library | Version | Loaded via | Size | Purpose |
|---------|---------|------------|------|---------|
| **Tailwind CSS** | CDN play | `<script src="cdn.tailwindcss.com">` | ~300KB | Utility-first CSS, configured inline in `base.html` |
| **HTMX** | 1.9.12 | unpkg CDN | 14KB | Auto-refresh partials (30s polling), lazy-load details |
| **Alpine.js** | 3.x | jsDelivr CDN | 15KB | Tabs, dropdowns, expand/collapse, mobile nav |
| **Chart.js** | 4.4.1 | jsDelivr CDN | 65KB | Time-series line charts, bar charts |
| **Kalam** | 700 weight | Google Fonts | — | Heading font (felt-tip marker look) |
| **Patrick Hand** | 400 weight | Google Fonts | — | Body font (handwritten, legible) |
| **Jinja2** | >=3.1 | Python dep | — | Server-side template rendering |

**Why no React/Vue:** Project values simplicity. No frontend engineers on the team. A JS build pipeline is unnecessary for 5 pages of tables and charts.

---

## Design System: "Hand-Drawn"

The dashboard uses a hand-drawn/sketchbook aesthetic. Every design decision should reinforce this.

### Colors

| Token | Hex | Tailwind class | Usage |
|-------|-----|----------------|-------|
| Paper (bg) | `#fdfbf7` | `bg-paper` | Page background |
| Pencil (fg) | `#2d2d2d` | `text-pencil`, `border-pencil` | Text, borders — never pure black |
| Erased | `#e5e0d8` | `border-erased`, `bg-erased` | Muted borders, dashed dividers |
| Marker (accent) | `#ff4d4d` | `text-marker`, `bg-marker` | Alerts, regressions, critical values |
| Pen (secondary) | `#2d5da1` | `text-pen`, `bg-pen` | Links, secondary accents, chart lines |
| Post-it | `#fff9c4` | `bg-postit` | Hover states, expanded details, feature cards |

These are configured in the Tailwind config in `base.html`:
```js
tailwind.config = { theme: { extend: { colors: { paper, pencil, erased, marker, pen, postit } } } }
```

### Severity Colors (CSS classes in base.html)
- `.severity-red` — `bg: #fee2e2, border: #ff4d4d` — Lock waits >30s, critical alerts
- `.severity-yellow` — `bg: #fef9c3, border: #eab308` — Lock waits >10s, warnings
- `.severity-green` — `bg: #dcfce7, border: #22c55e` — Healthy status

### Typography
- **Headings**: `font-heading` → `Kalam, cursive` (weight 700)
- **Body**: `font-body` → `Patrick Hand, cursive` (weight 400)
- All `<h1>-<h6>` tags get Kalam via CSS rule in `<style>` block
- Body gets Patrick Hand via CSS rule on `body`

### Borders — Wobbly (CRITICAL)

Never use standard `rounded-*` classes alone. Use these CSS classes defined in `base.html`:

```css
.wobbly    { border-radius: 255px 15px 225px 15px / 15px 225px 15px 255px; }
.wobbly-md { border-radius: 15px 225px 15px 255px / 255px 15px 225px 15px; }
.wobbly-sm { border-radius: 225px 15px 255px 15px / 15px 255px 15px 225px; }
```

Every card, button, badge, and container must use one of these. Vary them for visual diversity.

### Shadows — Hard Offset (No Blur)

Tailwind custom shadows (configured in `base.html`):
- `shadow-hard` → `4px 4px 0px 0px #2d2d2d` — Primary cards, alert sections
- `shadow-hard-sm` → `2px 2px 0px 0px #2d2d2d` — Buttons, small elements
- `shadow-hard-lg` → `8px 8px 0px 0px #2d2d2d` — Emphasized elements
- `shadow-hard-subtle` → `3px 3px 0px 0px rgba(45,45,45,0.1)` — Chart containers

**Never use blur shadows** (`shadow-md`, `shadow-lg`, etc.)

### Decorations (CSS pseudo-elements in base.html)

- `.tape` — Translucent gray bar at top center with slight rotation (simulates scotch tape)
- `.tack` — Red circular thumbtack at top center with pencil-lead border

### Visual Personality Rules

1. **Slight rotations** on cards: `style="transform: rotate(-0.5deg)"` — vary between -2deg and 2deg
2. **Dashed borders** for secondary dividers: `border-dashed border-erased`
3. **Paper texture background** on body: `radial-gradient(#e5e0d8 1px, transparent 1px)` at 24px spacing
4. **Nav underline** on active/hover: red marker squiggle via `.nav-link.active::after`
5. **Bounce animation** for decorative elements: `.bounce-gentle` (3s ease-in-out infinite)
6. **Tables**: `.sketch-table` class — 3px solid bottom on `th`, 1px dashed on `td`, postit hover on `tr`

### Button Pattern
```html
<button class="border-2 border-pencil px-3 py-1 wobbly-sm shadow-hard-sm
               hover:shadow-none hover:translate-x-[2px] hover:translate-y-[2px]
               transition-all duration-100 bg-white">
    Label
</button>
```
Active state: shadow disappears, translate increases to 4px (button "presses flat").

### Badge/Tag Pattern
```html
<span class="px-2 py-0.5 border border-pencil text-sm bg-blue-100"
      style="border-radius: 255px 15px 225px 15px / 15px 225px 15px 255px;">
    TAG
</span>
```

---

## File Structure

```
api/
  app.py                  # FastAPI factory — mounts static, Jinja2, routers
  routes.py               # Original raw data API (DO NOT MODIFY)
  dashboard_routes.py     # HTML page routes + HTMX partial routes
  dashboard_api.py        # /api/v1/ JSON endpoints (charts, tables)
  query_helpers.py        # Shared SQLite reader, time-range parsing

templates/
  base.html               # Master layout: nav, CDN links, Tailwind config, CSS classes
  dashboard/
    overview.html          # Health bar, KPI cards, QPS/threads charts, alerts
    queries.html           # Top-N query table, time range, sort, expandable rows
    locks.html             # Lock waits table, lock history chart, transactions
    schema.html            # DDL timeline, table sizes, index analysis (Alpine tabs)
    server.html            # 4 metric charts, wait events bar chart
  partials/
    health_bar.html        # HTMX-refreshed health indicator + quick stats
    active_alerts.html     # DDL changes, long txns, long locks (last 24h)
    current_locks.html     # Lock waits table with severity coloring
    active_transactions.html  # Transaction table with age highlighting
    query_detail.html      # Expandable: full SQL, stats, latency chart, EXPLAIN

static/
  css/dashboard.css        # Print styles only (everything else is Tailwind/inline)
  js/
    charts.js              # SeeQL.createChart(), SeeQL.fetchAndChart(), SeeQL.formatTime()
    dashboard.js           # HTMX event hooks (opacity during loads)
```

---

## Route Map

### HTML Pages (dashboard_routes.py)
| Route | Template | Data | Auto-refresh |
|-------|----------|------|-------------|
| `GET /dashboard` | `dashboard/overview.html` | threads, locks, buffer pool, top query, DDL, long txns | Health bar + alerts every 30s |
| `GET /dashboard/queries` | `dashboard/queries.html` | Top 25 queries (grouped by digest), regressions | No (manual reload) |
| `GET /dashboard/locks` | `dashboard/locks.html` | Current locks, active txns, metadata locks | Locks + txns every 30s |
| `GET /dashboard/schema` | `dashboard/schema.html` | DDL changes (20), table sizes (50), indexes (20 each) | No |
| `GET /dashboard/server` | `dashboard/server.html` | Wait events (top 10) | No (charts fetch via JS) |

Query params: `/dashboard/queries?range=1h|6h|24h|7d&sort=total_time_sec|avg_time_sec|exec_count|rows_examined|full_scans`

### HTMX Partials (dashboard_routes.py)
| Route | Template | Trigger |
|-------|----------|---------|
| `GET /dashboard/partials/health-bar` | `partials/health_bar.html` | `hx-trigger="every 30s"` |
| `GET /dashboard/partials/active-alerts` | `partials/active_alerts.html` | `hx-trigger="every 30s"` |
| `GET /dashboard/partials/current-locks` | `partials/current_locks.html` | `hx-trigger="every 30s"` |
| `GET /dashboard/partials/active-transactions` | `partials/active_transactions.html` | `hx-trigger="every 30s"` |
| `GET /dashboard/partials/query-detail/{digest}` | `partials/query_detail.html` | `hx-trigger="intersect once"` |

### JSON API (dashboard_api.py) — for Chart.js
| Route | Returns | Used by |
|-------|---------|---------|
| `GET /api/v1/queries/top?range=&sort=&limit=` | Top-N queries aggregated | — |
| `GET /api/v1/queries/{digest}/trend?range=` | Time-series per query | Query detail chart |
| `GET /api/v1/queries/{digest}/explain` | Latest EXPLAIN JSON | Query detail |
| `GET /api/v1/queries/regressions?threshold=` | 3x+ slower queries | — |
| `GET /api/v1/metrics/qps?range=` | QPS time-series | Overview + Server charts |
| `GET /api/v1/metrics/threads?range=` | `{running: [], connected: []}` | Overview + Server charts |
| `GET /api/v1/metrics/buffer-pool?range=` | hit_ratio time-series | Server chart |
| `GET /api/v1/metrics/innodb?range=&metrics=` | InnoDB counters | Server chart |
| `GET /api/v1/locks/history?range=&bucket=` | Bucketed lock counts | Locks chart |
| `GET /api/v1/schema/table-sizes?sort=` | Table sizes from latest snapshot | — |
| `GET /api/v1/server/wait-events/top?limit=` | Top wait events | Server page |
| `GET /api/v1/server/gcp-metrics?range=&metrics=` | GCP Cloud Monitoring | Server page |

---

## Chart.js Integration (static/js/charts.js)

Global namespace: `window.SeeQL`

### Key Functions

```js
SeeQL.defaultChartOptions()     // Returns Chart.js options with hand-drawn fonts/colors
SeeQL.formatTime(isoString)     // "2024-01-15T10:30:00" → "10:30 AM"
SeeQL.createChart(canvasId, config)  // Creates/replaces chart, tracks in SeeQL._charts
SeeQL.fetchAndChart(url, canvasId, opts)  // Fetch JSON + render line chart
```

### Chart Container Pattern (IMPORTANT)

Charts MUST be wrapped in a fixed-height container. Without this, Chart.js grows infinitely:

```html
<div style="position: relative; height: 220px;">
    <canvas id="my-chart"></canvas>
</div>
```

Chart.js uses `maintainAspectRatio: false` + `responsive: true`, so the container height controls the chart height.

### Chart Styling

- Grid lines: dashed (`borderDash: [4, 4]`), color `rgba(229, 224, 216, 0.5)`
- Tooltips: paper background, pencil text, 2px solid border, Kalam title font
- Tick labels: Patrick Hand font, pencil color
- Line charts: `tension: 0.3`, `borderWidth: 2.5`, `pointRadius: 1.5`
- Colors: pen blue `#2d5da1` (primary), marker red `#ff4d4d` (alerts), green `#22c55e` (healthy)

---

## HTMX Patterns

### Auto-refresh (polling)
```html
<div hx-get="/dashboard/partials/health-bar"
     hx-trigger="every 30s"
     hx-swap="innerHTML">
    {% include "partials/health_bar.html" %}
</div>
```
Initial render uses `{% include %}` for immediate content. HTMX replaces innerHTML on schedule.

### Lazy-load on visibility
```html
<div hx-get="/dashboard/partials/query-detail/{{ digest }}"
     hx-trigger="intersect once"
     hx-swap="innerHTML">
    <span>Loading...</span>
</div>
```

### Loading indicator (dashboard.js)
HTMX hooks set `opacity: 0.6` on target during request, restore to `1` after.

---

## Alpine.js Patterns

### Tabs (schema page)
```html
<div x-data="{ tab: 'changes' }">
    <button @click="tab = 'changes'" :class="tab === 'changes' ? 'active-style' : 'inactive-style'">
    <div x-show="tab === 'changes'" x-transition> ... </div>
</div>
```

### Expand/collapse (query rows)
```html
<tr x-data="{ expanded: false }">
    <td><button @click="expanded = !expanded">&#9654;</button></td>
</tr>
<tr x-show="expanded" x-transition>
    <td colspan="8"> ... lazy-loaded detail ... </td>
</tr>
```

### Mobile nav
```html
<body x-data="{ mobileNav: false }">
    <button @click="mobileNav = !mobileNav">
    <div x-show="mobileNav" x-transition> ... </div>
</body>
```

### Dropdown
```html
<div x-data="{ open: false }">
    <button @click="open = !open">Sort ▼</button>
    <div x-show="open" @click.outside="open = false" x-transition> ... </div>
</div>
```

---

## Data Flow

```
SQLite DB (WAL mode)
    ↓ (read-only shared connection via query_helpers._get_reader())
dashboard_routes.py → query_rows() / query_single()
    ↓
Jinja2 template rendering (server-side)
    ↓
HTML page → browser
    ↓
HTMX: polls partials every 30s (GET → HTML fragment → swap innerHTML)
Chart.js: fetches /api/v1/* JSON → renders canvas charts
Alpine.js: client-side tabs, toggles, dropdowns (no server calls)
```

### SQLite Query Pattern

All dashboard reads go through `query_helpers.py`:
- `_get_reader()` — shared read-only connection (PRAGMA query_only=ON), reused across requests
- `query_rows(sql, params)` → `list[dict]`
- `query_single(sql, params)` → `dict | None`
- `parse_time_range("1h"|"6h"|"24h"|"7d")` → `(start_iso, end_iso)`

The "latest snapshot" pattern uses:
```sql
WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM table_name)
```

---

## SQLite Tables Referenced by Dashboard

| Table | Dashboard usage | Key columns |
|-------|----------------|-------------|
| `processlist_snapshots` | Active thread count (overview KPI) | `snapshot_time`, `thread_id`, `time_sec`, `query` |
| `lock_wait_snapshots` | Lock waits table + history chart | `snapshot_time`, `wait_seconds`, `waiting_pid`, `blocking_pid`, `waiting_query`, `blocking_query` |
| `transaction_snapshots` | Active transactions table | `snapshot_time`, `trx_state`, `age_sec`, `pid`, `trx_query`, `rows_locked`, `rows_modified`, `isolation_level` |
| `query_digest_snapshots` | Top queries table + trend charts | `snapshot_time`, `digest`, `digest_text`, `exec_count`, `total_time_sec`, `avg_time_sec`, `rows_examined`, `rows_sent`, `full_scans`, `no_index_used` |
| `buffer_pool_snapshots` | Hit ratio KPI + chart | `snapshot_time`, `hit_ratio`, `dirty_pages`, `free_buffers` |
| `global_status_snapshots` | QPS chart, threads chart | `snapshot_time`, `variable_name`, `raw_value`, `delta_value`, `per_second` |
| `ddl_changes` | Schema change timeline | `detected_at`, `table_schema`, `table_name`, `change_type`, `old_ddl`, `new_ddl` |
| `schema_snapshots` | Table sizes | `snapshot_time`, `table_schema`, `table_name`, `table_rows`, `data_mb`, `index_mb` |
| `metadata_lock_snapshots` | Metadata locks table | `snapshot_time`, `object_schema`, `object_name`, `lock_type`, `lock_status` |
| `wait_event_snapshots` | Wait events bar chart | `snapshot_time`, `event_name`, `total_wait_sec` |
| `innodb_metric_snapshots` | InnoDB charts | `snapshot_time`, `metric_name`, `count_value` |
| `explain_captures` | Query detail EXPLAIN display | `captured_at`, `digest`, `explain_json` |
| `unused_index_snapshots` | Unused indexes list | `object_schema`, `table_name`, `index_name` |
| `redundant_index_snapshots` | Redundant indexes + DROP SQL | `table_schema`, `table_name`, `redundant_index_name`, `redundant_index_columns`, `dominant_index_name`, `sql_drop_index` |

---

## Performance Notes

- Pages load in **20-120ms** (all queries use indexed columns)
- Shared SQLite reader connection (`_get_reader()`) avoids per-request connection overhead (~35ms/connection)
- Digest text truncated to 120 chars in queries page (`SUBSTR(digest_text, 1, 120)`)
- DDL diffs truncated to 500 chars
- All list queries have LIMIT (20-50)
- Chart containers are fixed-height (220px) to prevent infinite vertical growth

---

## Template Conventions

1. **Every page extends `base.html`** and sets `{% block title %}`, `{% block content %}`, optionally `{% block scripts %}`
2. **Every page passes `page` variable** to template context for nav highlighting: `"page": "overview"|"queries"|"locks"|"schema"|"server"`
3. **Partials are plain HTML fragments** (no `{% extends %}`), meant for HTMX `innerHTML` swap
4. **Initial partial render uses `{% include %}`** inside the HTMX container for instant first load
5. **Card rotation values** should vary: alternate between negative and positive small angles
6. **Decoration classes** (`.tape`, `.tack`) should be used sparingly — 1-2 per page section
7. **Alpine `x-data`** goes on the nearest relevant element, not on body (except `mobileNav`)

---

## Known Gaps / TODO

- [ ] GCP metrics chart (data collection not yet implemented)
- [ ] Sparklines in KPI cards (currently just numbers)
- [ ] Table size growth sparklines on schema page
- [ ] Pagination for queries/schema tables (currently LIMIT-based)
- [ ] Dark mode (not planned — conflicts with hand-drawn aesthetic)
- [ ] Favicon
- [ ] Empty state illustrations (currently just text)
- [ ] Responsive testing on tablets
- [ ] Chart time range selector on overview page (currently hardcoded to 1h)

---

## How to Run

```bash
# Start with auto-reload (restarts on Python file changes)
SEEQL_ENV=dev venv/bin/uvicorn api.app:app --host 0.0.0.0 --port 8080 --reload

# Dashboard at http://localhost:8080/dashboard
# Template/JS/CSS changes: just refresh browser (no restart needed)
# Python changes: uvicorn auto-restarts
```

---

## How to Add a New Dashboard Widget

1. **Add SQL query** in `dashboard_routes.py` (for page data) or `dashboard_api.py` (for chart JSON)
2. **Add HTML** in the relevant `templates/dashboard/*.html` or create a new `templates/partials/*.html`
3. **Use design tokens**: wobbly borders, hard shadows, hand-drawn fonts, slight rotation
4. **Charts**: wrap `<canvas>` in `<div style="position: relative; height: 220px;">`, use `SeeQL.createChart()`
5. **Auto-refresh**: add `hx-get`, `hx-trigger="every 30s"`, `hx-swap="innerHTML"` + initial `{% include %}`
6. **Keep queries fast**: always LIMIT, always use indexed columns, use latest-snapshot pattern
