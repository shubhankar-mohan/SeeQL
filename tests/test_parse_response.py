"""Tests for the unified agent-response parser (Task 4.2 — parser correctness
batch: P1-2, P1-3, P1-18, P1-19, P3-5, P3-9).

Before this change, `agent/llm_agent.py` had two independently-maintained
copies of severity/section/confidence/addresses parsing (`_parse_and_store`
and `run_llm_analysis`), and the severity regex was stricter than everything
else in the file (`^#{2,3}\\s*Severity:` only — no bold, no ####, no loose
punctuation), so `**Severity:** critical`, `#### Severity: warning`, and
model preambles before the first header all silently produced the wrong
stored severity or leaked text into `findings`. Code-fence stripping used
per-line MULTILINE substitutions, which also stripped inner ```sql fences
nested inside a Recommendations section.

`agent.llm_agent.parse_agent_response` is now the single parser both
`_parse_and_store` and `run_llm_analysis` call.
"""

import logging

import pytest

import agent.llm_agent as la
from agent.llm_agent import parse_agent_response


# ---------------------------------------------------------------------------
# Step 1 table (brief-mandated): (severity, has_findings, has_recommendations,
# confidence) for each input. Every expected value below was hand-traced
# against the exact regexes in the brief before implementation existed, so
# this file is genuinely RED until `parse_agent_response` lands.
# ---------------------------------------------------------------------------
TABLE = [
    pytest.param(
        "### Severity: critical\n### Findings\nX\n### Recommendations\nY\n### Confidence: 0.8",
        "critical", True, True, 0.8,
        id="standard_format",
    ),
    pytest.param(
        "**Severity:** critical\n**Findings**\nX",
        "critical", True, False, None,
        id="bold_form_P1-2",
    ),
    pytest.param(
        "#### Severity: warning",
        "warning", True, False, None,
        id="four_hashes_P1-2",
    ),
    pytest.param(
        "Now I have a clear picture.\n\n### Severity: critical\n### Findings\nX",
        "critical", True, False, None,
        id="preamble_P3-9",
    ),
    pytest.param(
        "```markdown\n### Severity: info\n### Recommendations\n"
        "```sql\nCREATE INDEX i ON t(c);\n```\n```",
        "info", True, True, None,
        id="outer_fence_inner_sql_survives_P1-3",
    ),
    pytest.param(
        "### Findings: No change since analysis #4",
        "info", True, False, None,
        id="inline_idempotency_findings_P3-5",
    ),
    pytest.param(
        "### Confidence: .85",
        "info", True, False, 0.85,
        id="confidence_leading_dot_P1-19",
    ),
    pytest.param(
        "### Confidence: 1.7",
        "info", True, False, 1.0,
        id="confidence_clamped_P1-19",
    ),
]


@pytest.mark.parametrize(
    "text,exp_severity,exp_has_findings,exp_has_recommendations,exp_confidence", TABLE
)
def test_parse_agent_response_table(
    text, exp_severity, exp_has_findings, exp_has_recommendations, exp_confidence
):
    result = parse_agent_response(text)
    assert result["severity"] == exp_severity
    assert bool(result["findings"]) is exp_has_findings
    assert bool(result["recommendations"]) is exp_has_recommendations
    assert result["confidence"] == exp_confidence


# ---------------------------------------------------------------------------
# P3-9: model preamble before the first ### header must not leak into the
# cleaned text used for section extraction (and therefore not into findings).
# ---------------------------------------------------------------------------
class TestPreambleDropped:
    def test_preamble_absent_from_cleaned_and_findings(self):
        text = "Now I have a clear picture.\n\n### Severity: critical\n### Findings\nX"
        result = parse_agent_response(text)
        assert "Now I have a clear picture" not in result["cleaned"]
        assert "Now I have a clear picture" not in result["findings"]
        assert result["cleaned"].startswith("### Severity")


# ---------------------------------------------------------------------------
# P1-3: the outer fence is stripped via a single DOTALL match anchored to the
# start/end of the whole payload -- never a per-line MULTILINE sub -- so an
# inner ```sql fence nested inside a Recommendations section survives intact.
# ---------------------------------------------------------------------------
class TestInnerFenceSurvives:
    def test_inner_sql_fence_preserved_verbatim(self):
        text = (
            "```markdown\n"
            "### Severity: info\n"
            "### Recommendations\n"
            "```sql\n"
            "CREATE INDEX i ON t(c);\n"
            "```\n"
            "```"
        )
        result = parse_agent_response(text)
        assert "```sql" in result["recommendations"]
        assert "CREATE INDEX i ON t(c);" in result["recommendations"]
        assert result["recommendations"].rstrip().endswith("```")


# ---------------------------------------------------------------------------
# P3-5: "### Findings: <inline text>" must be captured by the section regex
# itself, not silently punted to the full-text fallback (which used to log a
# spurious warning on every idempotent "no change" analysis).
# ---------------------------------------------------------------------------
class TestInlineFindingsNoFallback:
    def test_exact_findings_body_no_header_leak(self):
        result = parse_agent_response("### Findings: No change since analysis #4")
        assert result["findings"] == "No change since analysis #4"

    def test_no_fallback_warning_logged(self, caplog):
        with caplog.at_level(logging.WARNING):
            parse_agent_response("### Findings: No change since analysis #4")
        assert not any(
            "storing full text as findings" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# P1-19 (addresses): a colon before the "#" ("Addresses incident: #7") must
# parse, not just the old bare "Addresses incident #7" form.
# ---------------------------------------------------------------------------
class TestAddressesIncidentTolerance:
    def test_colon_before_hash(self):
        result = parse_agent_response("### Addresses incident: #7")
        assert result["addresses_incident"] == 7

    def test_still_accepts_bare_form(self):
        result = parse_agent_response("### Addresses incident #7")
        assert result["addresses_incident"] == 7


# ---------------------------------------------------------------------------
# P1-18: prove the unification at the actual call sites, not just in the
# standalone function. Both `_parse_and_store` and `run_llm_analysis` used to
# carry their own copy of the (stricter) severity regex; a bold-form header
# that the old inline copy would have defaulted to "info" must now come out
# as "critical" through both call sites.
# ---------------------------------------------------------------------------
class TestParseAndStoreUsesSharedParser:
    def test_bold_severity_no_longer_defaults_to_info(self, mon_db, test_config):
        import config as config_module

        conn, db_path = mon_db
        config_module._config = test_config

        text = "**Severity:** critical\n**Findings**\nSomething bad.\n"
        analysis = la._parse_and_store(text, "routine", "summary", "default")

        assert analysis["severity"] == "critical"
        assert analysis["id"] is not None

        row = conn.execute(
            "SELECT severity FROM agent_analyses WHERE id = ?", (analysis["id"],)
        ).fetchone()
        assert row["severity"] == "critical"


class TestRunLlmAnalysisUsesSharedParser:
    def test_bold_severity_no_longer_defaults_to_info(self, mon_db, test_config, monkeypatch):
        import config as config_module

        conn, db_path = mon_db
        config_module._config = {**test_config, "agent": {}}

        monkeypatch.setattr(
            la, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )
        monkeypatch.setattr(
            la, "_run_anthropic_loop",
            # _run_anthropic_loop returns (text, truncated, tool_calls) since
            # P1-22, and now takes a 5th system_prompt arg (P3-2).
            lambda backend, max_tokens, max_rounds, prompt, system_prompt=None: (
                "**Severity:** critical\n**Findings**\nSomething bad.\n", False, 1,
            ),
        )

        result = la.run_llm_analysis("some prompt", analysis_type="replay", server_id="default")

        assert result["severity"] == "critical"
        assert result["analysis_id"] is not None

        row = conn.execute(
            "SELECT severity FROM agent_analyses WHERE id = ?", (result["analysis_id"],)
        ).fetchone()
        assert row["severity"] == "critical"
