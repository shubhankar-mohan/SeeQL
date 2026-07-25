"""Capture README screenshots from the running demo dashboard.

Usage:
    python scripts/seed_demo.py
    SEEQL_CONFIG=config/settings.demo.yaml SEEQL_API_PORT=8899 \
        python -m uvicorn api.app:app --port 8899 &
    python scripts/screenshot_demo.py [--base http://127.0.0.1:8899] [--out docs/screenshots]

Requires: pip install playwright && playwright install chromium

Captures each dashboard page at a 1600x1000 viewport with a 2x device scale
factor (3200x2000 PNGs — crisp on retina and in the GitHub README).
"""

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGES = [
    ("/dashboard", "overview.png"),
    ("/dashboard/queries", "queries.png"),
    ("/dashboard/todo", "action-center.png"),
    ("/dashboard/locks", "locks.png"),
    ("/dashboard/server", "server.png"),
    ("/dashboard/schema", "schema.png"),
]

# Chart.js animates for ~1s after data lands; wait past it so charts are drawn.
CHART_SETTLE_SECONDS = 3.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8899")
    ap.add_argument("--out", default="docs/screenshots")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=2,
        )
        for path, filename in PAGES:
            url = args.base + path
            page.goto(url, wait_until="networkidle", timeout=30_000)
            time.sleep(CHART_SETTLE_SECONDS)
            target = out_dir / filename
            page.screenshot(path=str(target))
            print(f"captured {url} -> {target}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
