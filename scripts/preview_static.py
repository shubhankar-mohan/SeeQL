#!/usr/bin/env python3
"""Preview the exported static demo site with the vercel.json rewrites applied.

Plain ``python -m http.server`` cannot resolve the app's clean fetch paths
(``/api/v1/metrics/qps?range=1h`` -> ``…/qps.json``; ``/dashboard/queries`` ->
``dashboard/queries/index.html``). This tiny server mirrors the rewrite +
clean-URL rules in ``vercel.json`` so the local preview matches production.

    python scripts/preview_static.py --dir dist --port 8001
"""
from __future__ import annotations

import argparse
import functools
import os
import re
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlsplit

# (regex on the request path, replacement file path). Mirrors vercel.json.
REWRITES = [
    (re.compile(r"^/$"), "/dashboard/index.html"),
    (re.compile(r"^/api/v1/metrics/([^/]+)$"), r"/api/v1/metrics/\1.json"),
    (re.compile(r"^/api/v1/locks/history$"), "/api/v1/locks/history.json"),
    (re.compile(r"^/api/v1/incidents/recent$"), "/api/v1/incidents/recent.json"),
    (re.compile(r"^/api/v1/investigations/recent$"), "/api/v1/investigations/recent.json"),
    (re.compile(r"^/api/v1/queries/([^/]+)/trend$"), r"/api/v1/queries/\1/trend.json"),
]


class RewriteHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        clean = urlsplit(path).path  # drop query string (vercel ignores it too)

        for pattern, repl in REWRITES:
            if pattern.match(clean):
                clean = pattern.sub(repl, clean)
                break
        else:
            # cleanUrls: a page path with no extension -> its index.html, if present.
            if not os.path.splitext(clean)[1]:
                candidate = os.path.join(self.directory, clean.strip("/"), "index.html")
                if os.path.isfile(candidate):
                    clean = clean.rstrip("/") + "/index.html"

        # Reuse the base translate_path against the rewritten clean path.
        return super().translate_path(clean)

    def guess_type(self, path):
        if path.endswith(".json") or "/api/" in path:
            return "application/json; charset=utf-8"
        return super().guess_type(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Preview the static demo with vercel rewrites.")
    ap.add_argument("--dir", default="dist")
    ap.add_argument("--port", type=int, default=8001)
    args = ap.parse_args()
    handler = functools.partial(RewriteHandler, directory=os.path.abspath(args.dir))
    httpd = HTTPServer(("127.0.0.1", args.port), handler)
    print(f"Serving {args.dir} with vercel rewrites at http://127.0.0.1:{args.port}/")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
