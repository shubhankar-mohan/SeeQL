# Incidents & replay

SeeQL's incident layer turns a stream of anomaly events into
timeline-based windows you can replay after the fact.

## Detection pipeline

```
medium loop  ──►  anomaly_detection  ──►  anomaly_events table
                                              │
                                              ▼
                                    incident_windows table
                                       (gap-clustering)
                                              │
                                              ▼
                            dashboard widget / Slack / seeql replay
```

1. Each medium-loop cycle, `alerting/anomaly.py::detect_anomalies()`
   computes z-scores for 8 metrics against 28-day same-hour-same-weekday
   baselines.
2. Anomalies above threshold persist to `anomaly_events` via
   `alerting/anomaly_store.py`.
3. `alerting/incidents.py::update_windows()` clusters events into
   `incident_windows`:
   - Anomalies within `incident_gap_minutes` (default 15) of the last
     event extend an open window.
   - Gaps larger than that start a new window.
   - Windows longer than `incident_max_duration_minutes` (default 120)
     are force-closed to prevent runaway.
4. New incidents notify Slack; extensions do not.

Tuning lives in `alerting`:

```yaml
alerting:
  incident_gap_minutes: 15
  incident_max_duration_minutes: 120
```

## Data model

`incident_windows` schema (shipped in `storage/schema.sql`):

| Column | Type | Meaning |
|--------|------|---------|
| `id` | INTEGER PK | Incident id — pass to `seeql replay --incident` |
| `server_id` | TEXT | Which MySQL target |
| `start_time` / `end_time` | TEXT (ISO) | Window bounds |
| `severity` | TEXT | Max severity across constituent events |
| `involved_metrics` | JSON array | Distinct metric names |
| `event_count` | INTEGER | Anomaly events in the window |
| `status` | TEXT | `detected` / `analyzed` / `resolved` |

`anomaly_events` holds the raw stream.

## `seeql replay`

Rebuilds a chronological timeline from every stored snapshot inside
a window (or arbitrary range) and — if an LLM is configured — asks it
to narrate the root cause.

```bash
seeql replay --latest                 # most recent incident
seeql replay --incident 42            # by id
seeql replay --from 2026-04-10T03:00 --to 2026-04-10T04:00

# Multi-server: pin a target
seeql replay --latest --server db-prod-west
```

The timeline blends:

- Anomaly events (from `anomaly_events`)
- Lock waits (from `lock_wait_snapshots`)
- DDL changes (from `ddl_changes`)
- Metric deltas (from `global_status_snapshots`)
- Process state (from `processlist_snapshots`)
- Active transactions (from `active_transactions`)

Events are ordered by timestamp and rendered as a Markdown list.

## LLM narration

If `agent.enabled: true` and a backend is configured, replay also
feeds the timeline through
[`INCIDENT_INVESTIGATOR_PROMPT`](../agent/prompts.py) and prints the
root-cause analysis.

Without a backend, replay falls back to timeline-only — you still get
the chronological reconstruction, just no narration. This is what
makes `seeql replay --latest` useful even in environments without LLM
credentials.

## Dashboard widget

The overview page shows a "Recent incidents" panel with HTMX
auto-refresh (every 30 s) and ARIA live regions so screen readers
announce new incidents. The widget lists start/end, duration, severity,
and involved metrics; clicking an incident navigates to the replay
view.

## Output

Replay writes nothing automatically — the LLM analysis prints to
stdout. Save it yourself:

```bash
seeql replay --incident 42 > reports/incident-42.md
```

Auto-save-to-reports is tracked as a P2 todo (see
[TODOS.md](../TODOS.md#auto-generated-postmortem-markdown-files)).

## Multi-server isolation

Anomaly detection, incident windowing, and replay all respect
`server_id`. A spike on `db-prod-west` doesn't create false-positive
incidents on `db-prod-east`. Cooldowns on the alerting side are
likewise namespaced.

## Related

- [Alerting](alerting.md) — how anomaly detection fires as a rule
- [Agent](agent.md) — the LLM that narrates replays
- [CLI — `seeql replay`](cli.md#seeql-replay)
- [CLI — `seeql incidents list`](cli.md#seeql-incidents-list)
- [E010 — invalid time range](errors/E010.md)
