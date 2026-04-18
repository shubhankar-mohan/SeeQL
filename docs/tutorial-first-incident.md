# Tutorial: your first incident (coming in v0.2)

> **Status:** This walkthrough depends on `seeql demo` — a
> bundled-incident seed command that is planned but not yet
> implemented (tracked in
> [PLAN.md §2.3](../PLAN.md) and [IMPLEMENTATION.md §2.3](../IMPLEMENTATION.md)).
> When it ships in v0.2, this page will walk you end-to-end through:
>
> - `seeql demo` → seed a synthetic 30-day incident history against a
>   local throwaway SQLite DB
> - `seeql incidents list` → see the seeded incidents
> - `seeql replay --latest` → watch the timeline get reconstructed
>   and the LLM narrate the root cause
> - Expected output snippets for each step so you can verify you're
>   on-track

Until then, to dogfood the flow against your own data:

1. Start SeeQL pointed at a real MySQL (`seeql serve`).
2. Wait at least a few hours so anomaly baselines warm up.
3. Generate some real load — a batch aggregation while transactional
   writes are happening will reliably trip `lock_cascade`:

   ```sql
   -- Session 1 (hold a lock)
   BEGIN;
   UPDATE orders SET status='pending' WHERE id=1;

   -- Session 2 (generate waiters)
   SELECT * FROM orders WHERE id=1 FOR UPDATE;   -- blocks
   ```

4. Visit `/incidents` in the dashboard or run:

   ```bash
   seeql incidents list
   seeql replay --latest
   ```

Meanwhile the demo command is tracked — see
[TODOS.md](../TODOS.md) — and will land with a self-contained
synthetic incident you can explore without touching production.

## Related

- [Incidents & replay](incidents.md)
- [Alerting rules](alerting.md)
- [CLI — `seeql replay`](cli.md#seeql-replay)
