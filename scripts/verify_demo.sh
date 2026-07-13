#!/usr/bin/env bash
# Boot the Grand Line demo dashboard against the seeded SQLite DB, verify every
# page + chart renders populated, then stop it. Pure-Python HTTP checks (no curl).
#
# Requires a Python with the web extras installed (fastapi, uvicorn, jinja2).
# By default it uses the repo's ./venv; override with SEEQL_PYTHON, e.g.
#   SEEQL_PYTHON=python3 bash scripts/verify_demo.sh     # if seeql[api] is on PATH
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${SEEQL_PYTHON:-./venv/bin/python}"
PORT="${SEEQL_API_PORT:-8899}"
export SEEQL_CONFIG="config/settings.demo.yaml"

if [ ! -x "$PYTHON" ] && ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "ERROR: '$PYTHON' not found. Set SEEQL_PYTHON to a Python that has the web"
  echo "extras (pip install 'seeql[api]'), e.g. SEEQL_PYTHON=python3 bash scripts/verify_demo.sh"
  exit 2
fi

"$PYTHON" scripts/seed_demo.py

"$PYTHON" -m uvicorn api.app:app --host 127.0.0.1 --port "$PORT" >/tmp/seeql_demo_verify.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

BASE="http://127.0.0.1:${PORT}" "$PYTHON" - <<'PY'
import json, os, sys, time, urllib.request
base = os.environ["BASE"]

# wait for uvicorn to accept connections (max ~30s)
up = False
for _ in range(60):
    try:
        urllib.request.urlopen(base + "/dashboard", timeout=2); up = True; break
    except Exception:
        time.sleep(0.5)
if not up:
    print("FAIL  server did not boot (see /tmp/seeql_demo_verify.log)"); sys.exit(1)

fail = 0

def body(path):
    return urllib.request.urlopen(base + path, timeout=8).read().decode("utf-8", "replace")

def check_html(path, want, reject):
    global fail
    try:
        b = body(path)
    except Exception as exc:
        print(f"FAIL  {path}  (HTTP error: {exc})"); fail += 1; return
    if reject and reject.lower() in b.lower():
        print(f"FAIL  {path}  (found empty-state: {reject!r})"); fail += 1; return
    if want and want.lower() not in b.lower():
        print(f"FAIL  {path}  (missing expected: {want!r})"); fail += 1; return
    print(f"OK    {path}")

def check_series(path, min_points):
    global fail
    try:
        data = json.loads(body(path))
    except Exception as exc:
        print(f"FAIL  {path}  (error: {exc})"); fail += 1; return
    if isinstance(data, dict):
        n = min((len(v) for v in data.values() if isinstance(v, list)), default=0)
    else:
        n = len(data)
    if n < min_points:
        print(f"FAIL  {path}  ({n} points < {min_points})"); fail += 1; return
    print(f"OK    {path}  ({n} points)")

# Pages must render populated (no WAITING / empty-state banners).
check_html("/dashboard", "grandline", "waiting")
check_html("/dashboard/queries", "pirates", "no query data")
check_html("/dashboard/locks", "bounties", None)
check_html("/dashboard/schema", "pirates", None)
check_html("/dashboard/server", "wait/", "no wait event data")
check_html("/dashboard/todo", None, None)
check_html("/dashboard/partials/active-alerts", None, "all quiet")

# Charts must have data at BOTH the 1h (overview) and 24h ranges.
check_series("/api/v1/metrics/qps?range=1h", 3)
check_series("/api/v1/metrics/qps?range=24h", 20)
check_series("/api/v1/metrics/threads?range=1h", 3)
check_series("/api/v1/metrics/buffer-pool?range=24h", 20)
check_series("/api/v1/metrics/innodb?range=24h", 10)
check_series("/api/v1/locks/history?range=24h&bucket=5m", 3)
check_series("/api/v1/incidents/recent?limit=5", 1)
check_series("/api/v1/investigations/recent?limit=8", 1)

if fail:
    print(f"\nDEMO VERIFY: FAILED ({fail} check(s))"); sys.exit(1)
print(f"\nDEMO VERIFY: OK — dashboard populated and ready to screenshot at {base}/dashboard")
PY