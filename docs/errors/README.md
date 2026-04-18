# SeeQL error catalog

SeeQL raises structured errors with a short code (`E001` – `E010`) that
maps to a page in this directory. Every error dumps a link to its page,
so a user who hits `error[E002]: performance_schema is disabled` can
jump straight to the fix.

The catalog is intentionally small — the goal is "every new user hits a
clear message for the top 10 things that can go wrong," not exhaustive
enumeration. New codes are added sparingly.

| Code | Summary | Severity |
|------|---------|----------|
| [E001](E001.md) | MySQL authentication failed | blocking |
| [E002](E002.md) | `performance_schema` is disabled | blocking |
| [E003](E003.md) | GCP Application Default Credentials not configured | optional |
| [E004](E004.md) | Invalid config file | blocking |
| [E005](E005.md) | Required Cloud SQL flag is missing or wrong | blocking (on GCP) |
| [E006](E006.md) | MySQL connection timeout | blocking |
| [E007](E007.md) | Permission denied — missing grants | blocking |
| [E008](E008.md) | SQLite monitoring database is full or not writable | blocking |
| [E009](E009.md) | LLM API credentials invalid | optional |
| [E010](E010.md) | Invalid time range for replay | CLI-only |

**Source of truth:** [`seeql/errors.py`](../../seeql/errors.py). If you
edit that catalog, update the page here too.
