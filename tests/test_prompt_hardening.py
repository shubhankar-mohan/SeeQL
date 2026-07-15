"""
Section E prompt hardening tests.

Asserts that SYSTEM_PROMPT contains the Section E directives (SQL/identifier
safety, index decision-tree, severity-by-absolute-danger, no hollow
non-actions, non-index lens) and the machine-contract output fields
(### Confidence:, ### Addresses incident #N) that agent/llm_agent.py's
_extract_confidence / _extract_addresses_incident regexes consume.
"""
from agent import prompts as p


def test_forbids_digest_text_to_explain_query():
    s = p.SYSTEM_PROMPT.lower()
    assert "digest_text" in s and "not runnable" in s
    assert "run_explain" in s and "search_slow_log" in s


def test_identifier_resolution_rule():
    s = p.SYSTEM_PROMPT.lower()
    assert "after the dot" in s or "schema.table" in s


def test_index_decision_tree():
    s = p.SYSTEM_PROMPT.lower()
    for kw in ("table size", "use index", "over-fetch", "generated column"):
        assert kw in s


def test_absolute_danger_severity():
    s = p.SYSTEM_PROMPT.lower()
    assert "absolute" in s and "cpu" in s and "memory" in s


def test_machine_contract_confidence_and_addresses():
    assert "### Confidence:" in p.SYSTEM_PROMPT
    assert "Addresses incident #" in p.SYSTEM_PROMPT


def test_no_hollow_nonactions():
    assert "refer to previous analyses" in p.SYSTEM_PROMPT.lower()


def test_non_index_lens():
    assert "non-index" in p.SYSTEM_PROMPT.lower() or "cannot fix" in p.SYSTEM_PROMPT.lower()
