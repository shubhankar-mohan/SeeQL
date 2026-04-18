# TODOS.md

> **Completed-and-removed tracker:** `git init` + `.gitignore`, CLAUDE.md refresh, multi-server alert filtering, SIGTERM graceful shutdown, and per-table retention overrides are all shipped — see `CHANGELOG.md` for details.

## P1

_(empty — all P1 items shipped. Next-up items are in P2.)_

## P2

### Auto-generated postmortem Markdown files
- **What:** After `seeql replay`, save the LLM root cause analysis to `reports/incident-{id}-{date}.md`
- **Why:** Creates a shareable artifact for Slack or ticket systems. Without this, replay output is terminal-only and disappears.
- **Effort:** S (human: 2h / CC: 10 min)
- **Depends on:** Incident replay CLI built

### Dashboard accessibility (a11y)
- **What:** Implement a11y requirements from the Dashboard Design Specifications in PLAN.md: (1) `aria-live="polite"` on all HTMX auto-refresh containers, (2) skip-to-content link before nav, (3) `aria-label` on all Chart.js canvas elements, (4) keyboard-focusable info tooltips (`tabindex="0"`, `:focus` CSS), (5) verify 44px min touch targets on pagination/buttons
- **Why:** Zero ARIA landmarks means screen readers can't use the dashboard. Auto-refresh regions (health bar, alerts, locks) update silently. Info tooltips are hover-only.
- **Effort:** S (human: 4h / CC: 15 min) — can be done incrementally, each item is independent
- **Depends on:** Nothing

### First-run onboarding state
- **What:** Detect zero-data state (no rows in `query_digest_snapshots`) and show onboarding messages instead of false-positive "healthy" states. See Dashboard Design Specifications in PLAN.md for exact messaging per component.
- **Why:** Fresh deploy shows "All clear — no emergencies" when no data has been collected. DBA thinks agent is working when nothing has arrived yet.
- **Effort:** S (human: 2h / CC: 10 min)
- **Depends on:** Nothing

### Create DESIGN.md via /design-consultation
- **What:** Extract the sketch design system from base.html and scattered templates into a standalone DESIGN.md. Document all color tokens, font usage, border-radius variants, shadow hierarchy, decorative element rules, and component patterns.
- **Why:** Design system is implicit in code. New pages risk drifting from the aesthetic without a reference doc. The PLAN.md design specs are a stop-gap.
- **Effort:** S (human: 1 day / CC: 30 min via /design-consultation)
- **Depends on:** Nothing

### Stripe-tier JSON error format for API
- **What:** Every API error response follows `{error: {type, code, message, param, doc_url}}` format (Stripe-style Tier 3 errors).
- **Why:** Server-to-server consumers (Prometheus scrapers, CI integrations, custom dashboards) need programmatic error handling, not HTML. SDK clients can branch on error codes.
- **Effort:** M (human: 2 days / CC: 1 hour)
- **Depends on:** Top 10 CLI error codes (E001-E010) shipping first so the catalog is stable
- **Context:** Pass 3 of /plan-devex-review deferred this. The CLI gets Tier 2 (Rust-style) errors. The API equivalent (Tier 3) is deferred until there are external API consumers. Not urgent for internal use.

## P3

### Incident comparison tool
- **What:** `seeql incidents compare 3 7` — diff two incidents side by side
- **Why:** Recurring incidents have similar patterns. Comparing them reveals whether fixes worked.
- **Effort:** M (human: 3 days / CC: 20 min)
- **Depends on:** Incident replay + accumulated incident data

### Counterfactual analysis in replay
- **What:** "What if we had killed PID 812 at T+15s?" simulation using stored data
- **Why:** Helps teams learn from incidents by exploring alternative actions
- **Effort:** M (human: 1 week / CC: 30 min)
- **Depends on:** Incident replay with reliable output quality

### Hosted demo playground (seeql.dev/demo)
- **What:** Deploy a public web playground where visitors click through pre-loaded incidents without installing anything. Netdata-style live demo site.
- **Why:** Champion-tier DX move. Reduces TTHW to ~10 seconds. Zero-friction evaluation for open-source launch.
- **Effort:** L (human: 2-3 weeks / CC: 1 day)
- **Depends on:** `seeql demo` command shipping first, domain registration, hosting decision
- **Context:** Deferred from Pass 1 of /plan-devex-review. Revisit after `seeql demo` proves traction.

### Opt-in anonymous telemetry
- **What:** Ship a tiny telemetry endpoint that tracks `seeql demo` runs, `seeql check` pass/fail, setup completion time. No IPs, no DB contents, no query text. First run asks permission.
- **Why:** Without telemetry, TTHW targets are guesses. With it, you know if the 2-5 min target is being hit in the wild. Find friction points users don't report.
- **Effort:** M (human: 3 days / CC: 1 hour)
- **Depends on:** Public launch + meaningful adoption volume (~100+ stars or observed usage)
- **Context:** Deferred from Pass 8 of /plan-devex-review in favor of GitHub signal. Add when volume makes the signal valuable.

### Hosted docs site with Algolia DocSearch
- **What:** Deploy `docs/` as a static site (Docusaurus or MkDocs Material) at `docs.seeql.dev` with full-text search via Algolia DocSearch (free for OSS).
- **Why:** Searchable hosted docs is where real tools live. Particularly important for error code lookups. Professional appearance for open-source launch.
- **Effort:** M (human: 3 days / CC: 2 hours)
- **Depends on:** `docs/` directory structure landing first
- **Context:** Deferred from Pass 4 of /plan-devex-review. Do after OSS launch when docs content has stabilized.
