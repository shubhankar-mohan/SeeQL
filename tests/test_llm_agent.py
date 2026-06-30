"""Tests for the LLM agent provider layer (agent/llm_agent.py).

These guard the bug class that kept shipping undetected: provider selection,
SDK response-shape parsing, and section extraction. Before this file, the
provider loops and `_detect_backend` had zero direct coverage — the higher-level
tests all mocked `run_llm_analysis` wholesale.
"""

import logging
from unittest.mock import MagicMock, patch

from agent import llm_agent


class TestDetectBackend:
    """Provider selection is model-name-driven; SeeQL supports only Claude
    (API + Vertex) and Gemini (Vertex)."""

    def test_claude_with_gcp_creds_uses_vertex(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/fake.json")
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {"project_id": "p"}})
        b = llm_agent._detect_backend({"model": "claude-opus-4-6"})
        assert b is not None
        assert b["type"] == "vertex-claude"
        assert b["model"] == "claude-opus-4-6"

    def test_claude_with_api_key_no_gcp_uses_anthropic(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        b = llm_agent._detect_backend(
            {"model": "claude-opus-4-6", "anthropic_api_key": "sk-real-key"}
        )
        assert b is not None
        assert b["type"] == "anthropic"
        assert b["model"] == "claude-opus-4-6"

    def test_gemini_with_gcp_uses_vertex_gemini(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/fake.json")
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {"project_id": "p"}})
        b = llm_agent._detect_backend({"model": "gemini-2.0-flash"})
        assert b is not None
        assert b["type"] == "gemini"
        assert b["model"] == "gemini-2.0-flash"

    def test_unsupported_model_is_coerced_and_warns(self, monkeypatch, caplog):
        """An unsupported model (e.g. gpt-4o) is silently runnable today; make
        sure the user is at least WARNED that their choice was swapped."""
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        with caplog.at_level(logging.WARNING):
            b = llm_agent._detect_backend(
                {"model": "gpt-4o", "anthropic_api_key": "sk-real-key"}
            )
        assert b is not None
        assert b["type"] == "anthropic"
        assert b["model"].startswith("claude")
        assert any("not a supported model" in r.message for r in caplog.records)

    def test_unsupported_model_coerced_to_gemini_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/fake.json")
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {"project_id": "p"}})
        with caplog.at_level(logging.WARNING):
            b = llm_agent._detect_backend({"model": "gpt-4o"})
        assert b is not None
        assert b["type"] == "gemini"
        assert b["model"] == "gemini-2.0-flash"
        assert any("not a supported model" in r.message for r in caplog.records)

    def test_no_creds_returns_none(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        # Unsubstituted ${...} placeholder must not count as a real key.
        b = llm_agent._detect_backend(
            {"model": "claude-x", "anthropic_api_key": "${ANTHROPIC_API_KEY}"}
        )
        assert b is None


class TestGeminiResponseShapes:
    """Gemini can return empty/None candidates (safety/recitation blocks,
    MAX_TOKENS with no content) — the tool loop must not IndexError."""

    def _run_with_response(self, fake_response):
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response
        with patch("google.genai.Client", return_value=fake_client):
            return llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100,
                max_rounds=3,
                user_msg="hi",
            )

    def test_empty_candidates_does_not_crash(self):
        resp = MagicMock()
        resp.candidates = []
        assert self._run_with_response(resp) == ""

    def test_none_candidates_does_not_crash(self):
        resp = MagicMock()
        resp.candidates = None
        assert self._run_with_response(resp) == ""

    def test_text_only_response_returns_text(self):
        part = MagicMock()
        part.text = "All healthy."
        part.function_call = None
        content = MagicMock()
        content.parts = [part]
        cand = MagicMock()
        cand.content = content
        resp = MagicMock()
        resp.candidates = [cand]
        assert self._run_with_response(resp) == "All healthy."


class TestSplitFindingsRecommendations:
    """The replay/investigator prompt uses a singular `### Recommendation`
    header with no `### Findings`; parsing must still populate both columns."""

    def test_standard_agent_format(self):
        text = "### Findings\nA bad query.\n\n### Recommendations\nAdd an index."
        findings, recs = llm_agent._split_findings_recommendations(text)
        assert "bad query" in findings
        assert "Add an index" in recs

    def test_replay_format_singular_recommendation(self):
        text = (
            "### Executive summary\nLock cascade.\n\n"
            "### Root cause\nBatch job took row locks.\n\n"
            "### Recommendation\nMove the batch off-peak."
        )
        findings, recs = llm_agent._split_findings_recommendations(text)
        assert "Move the batch off-peak" in recs       # recommendation populated
        assert "Root cause" in findings                # findings is everything before it
        assert "Recommendation" not in findings

    def test_unparseable_falls_back_to_findings(self):
        text = "Just a blob with no headers at all."
        findings, recs = llm_agent._split_findings_recommendations(text)
        assert findings == text
        assert recs == ""
