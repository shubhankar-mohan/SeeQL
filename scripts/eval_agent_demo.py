"""Empirical eval: run the SeeQL LLM agent on the seeded demo incident and dump artifacts.

Ground truth is planted by scripts/seed_demo.py (bounty-batch lock cascade), so
the output can be judged for grounding: did the agent find pid 5099, the
`UPDATE bounties … JOIN pirates` transaction, and the lock-driven b5956bf0
regression?

Usage:
    python scripts/seed_demo.py                        # fresh incident first
    python scripts/eval_agent_demo.py <model> <label>  # e.g. gemini-2.5-flash flash

Prereqs: credentials for the chosen backend — GOOGLE_APPLICATION_CREDENTIALS +
SEEQL_EVAL_GCP_PROJECT for Vertex models, or ANTHROPIC_API_KEY for claude-*
via the Anthropic API. Claude on Vertex serves from us-east5 (auto-set below).

Writes <label>_state_report.md, <label>_raw.md, <label>_summary.json to the
directory you run it from.
"""
import json
import logging
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.getcwd()
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ["SEEQL_CONFIG"] = "config/settings.demo.yaml"

logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s: %(message)s")
# Quiet the noisy libs, keep agent tool-call lines
for noisy in ("httpx", "urllib3", "google", "google_genai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

model, label = sys.argv[1], sys.argv[2]

from config import get_config  # noqa: E402

cfg = get_config()
# The demo config has no gcp section; allow Vertex backends via env override.
eval_project = os.environ.get("SEEQL_EVAL_GCP_PROJECT")
if eval_project:
    cfg.setdefault("gcp", {})["project_id"] = eval_project
agent_cfg = cfg.setdefault("agent", {})
agent_cfg.update({"enabled": True, "skip_quiet": False, "model": model, "max_tool_rounds": 15})
if model.startswith("claude") and eval_project:
    cfg["gcp"]["vertex_region"] = "us-east5"  # Claude on Vertex serves from us-east5

DB = os.path.join(REPO, "data/grandline_demo.db")


def incident_state():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, status, analysis_id FROM incident_windows ORDER BY id"
    ).fetchall()
    conn.close()
    return {r[0]: (r[1], r[2]) for r in rows}


before = incident_state()

from agent.state_builder import build_state_report  # noqa: E402

report = build_state_report(server_id="grandline-prod")
state_md = report.to_markdown()
with open(os.path.join(SCRATCH, f"{label}_state_report.md"), "w") as f:
    f.write(state_md)
print(f"STATE REPORT: {len(state_md)} chars -> {label}_state_report.md")

from agent.llm_agent import run_analysis  # noqa: E402

t0 = time.time()
res = run_analysis("routine", server_id="grandline-prod")
elapsed = time.time() - t0

after = incident_state()

if res is None:
    print("RESULT: None (skipped or failed — see log above)")
    sys.exit(1)

raw = res.get("raw_response") or ""
with open(os.path.join(SCRATCH, f"{label}_raw.md"), "w") as f:
    f.write(raw)

# Read back the stored row to verify what actually persisted
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT id, severity, outcome_notes, LENGTH(findings) flen, LENGTH(recommendations) rlen "
    "FROM agent_analyses ORDER BY id DESC LIMIT 1"
).fetchone()
conn.close()

summary = {
    "model": model,
    "elapsed_sec": round(elapsed, 1),
    "raw_chars": len(raw),
    "parsed_severity": res.get("severity"),
    "stored_row": dict(row) if row else None,
    "incident_diff": {
        str(k): {"before": before.get(k), "after": after.get(k)}
        for k in sorted(set(before) | set(after))
        if before.get(k) != after.get(k)
    },
}
with open(os.path.join(SCRATCH, f"{label}_summary.json"), "w") as f:
    json.dump(summary, f, indent=2, default=str)

print("=" * 70)
print(json.dumps(summary, indent=2, default=str))
print("=" * 70)
print("RAW RESPONSE (first 3500 chars):")
print(raw[:3500])
