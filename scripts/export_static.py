#!/usr/bin/env python3
"""Freeze the running SeeQL demo dashboard into static files under dist/.

Run (with the demo serve already up on :8899):
    python scripts/export_static.py --base http://127.0.0.1:8899 --out dist
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.request
from urllib.parse import urlsplit

BASE_DIR = os.path.join(os.path.dirname(__file__), "..")

PAGES = [
    "/dashboard", "/dashboard/queries", "/dashboard/locks",
    "/dashboard/schema", "/dashboard/server", "/dashboard/todo",
]
PARTIALS = [
    "/dashboard/partials/health-bar",
    "/dashboard/partials/active-alerts",
    "/dashboard/partials/active-transactions",
    "/dashboard/partials/current-locks",
]
# Chart/panel JSON with the default params each page requests on load.
API_STATIC = [
    "/api/v1/metrics/qps?range=1h",
    "/api/v1/metrics/threads?range=1h",
    "/api/v1/metrics/buffer-pool?range=24h",
    "/api/v1/metrics/innodb?range=24h",
    "/api/v1/locks/history?range=24h&bucket=5m",
    "/api/v1/incidents/recent?limit=5",
    "/api/v1/investigations/recent?limit=8",
]


def local_path(url_path: str) -> str:
    """Map a request path (+query) to a flat file path under the out dir."""
    parts = urlsplit(url_path)
    path = parts.path.strip("/")
    if path.startswith("api/"):
        return f"{path}.json"                 # query string dropped
    if "/partials/" in ("/" + path):
        return path                            # HTMX fetches this exact path
    return f"{path}/index.html"                # pages get clean-URL dirs


def _get(base: str, url_path: str) -> bytes:
    with urllib.request.urlopen(base + url_path, timeout=30) as resp:
        return resp.read()


def discover_digests(base: str) -> list[str]:
    """Read the queries API to learn which per-digest URLs to freeze."""
    try:
        raw = _get(base, "/api/v1/queries?range=24h&limit=50")
        data = json.loads(raw)
        rows = data.get("queries", data if isinstance(data, list) else [])
        digests = [r["digest"] for r in rows if isinstance(r, dict) and r.get("digest")]
        if digests:
            return digests
    except Exception:
        pass
    # Fallback: the known seeded digests.
    return ["7107e33a", "abf87900", "0598ca31", "f0998abc", "c19a1fa7",
            "d4410aa2", "e7712bb0", "a8890c14", "b5956bf0"]


def api_urls(digests: list[str]) -> list[str]:
    urls = list(API_STATIC)
    for dg in digests:
        urls.append(f"/api/v1/queries/{dg}/trend?range=24h")
    return urls


def _write(out_dir: str, rel_path: str, body: bytes) -> str:
    dest = os.path.join(out_dir, rel_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(body)
    return rel_path


def export(base: str, out_dir: str) -> list[str]:
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []

    digests = discover_digests(base)
    partials = list(PARTIALS) + [f"/dashboard/partials/query-detail/{d}" for d in digests]

    for p in PAGES + partials + api_urls(digests):
        try:
            body = _get(base, p)
        except Exception as exc:  # keep going; report at the end
            print(f"WARN  {p}  ({exc})")
            continue
        written.append(_write(out_dir, local_path(p), body))

    # copy static assets verbatim
    static_src = os.path.join(BASE_DIR, "static")
    if os.path.isdir(static_src):
        shutil.copytree(static_src, os.path.join(out_dir, "static"))
        written.append("static/")

    # root -> overview
    with open(os.path.join(out_dir, "index.html"), "wb") as fh:
        fh.write(_get(base, "/dashboard"))
    written.append("index.html")

    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Freeze the demo dashboard to static files.")
    ap.add_argument("--base", default="http://127.0.0.1:8899")
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "dist"))
    args = ap.parse_args()
    written = export(args.base, args.out)
    print(f"Exported {len(written)} paths to {args.out}")


if __name__ == "__main__":
    main()
