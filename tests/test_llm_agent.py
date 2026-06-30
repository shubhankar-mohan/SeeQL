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
        assert any("no matching backend" in r.message for r in caplog.records)

    def test_unsupported_model_coerced_to_gemini_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/fake.json")
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {"project_id": "p"}})
        with caplog.at_level(logging.WARNING):
            b = llm_agent._detect_backend({"model": "gpt-4o"})
        assert b is not None
        assert b["type"] == "gemini"
        assert b["model"] == "gemini-2.0-flash"
        assert any("no matching backend" in r.message for r in caplog.records)

    def test_no_creds_returns_none(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        # Unsubstituted ${...} placeholder must not count as a real key.
        b = llm_agent._detect_backend(
            {"model": "claude-x", "anthropic_api_key": "${ANTHROPIC_API_KEY}"}
        )
        assert b is None


class TestOpenAIBackend:
    """OpenAI + any OpenAI-compatible endpoint (custom base_url)."""

    def test_explicit_provider_openai_with_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        b = llm_agent._detect_backend(
            {"provider": "openai", "model": "gpt-4o", "openai_api_key": "sk-x"}
        )
        assert b is not None
        assert b["type"] == "openai"
        assert b["model"] == "gpt-4o"
        assert b["api_key"] == "sk-x"

    def test_openai_compatible_base_url_only(self, monkeypatch):
        """A custom OpenAI-compatible server (e.g. Ollama) — base_url, no key."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        b = llm_agent._detect_backend(
            {"provider": "openai", "model": "llama3.1",
             "openai_base_url": "http://localhost:11434/v1"}
        )
        assert b is not None
        assert b["type"] == "openai"
        assert b["base_url"] == "http://localhost:11434/v1"

    def test_gpt_model_name_inferred(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        b = llm_agent._detect_backend({"model": "gpt-4o", "openai_api_key": "sk-x"})
        assert b is not None and b["type"] == "openai"

    def test_provider_openai_without_creds_returns_none(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        assert llm_agent._detect_backend({"provider": "openai", "model": "gpt-4o"}) is None

    def test_openai_env_var_key_picked_up(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        b = llm_agent._detect_backend({"model": "gpt-4o"})
        assert b is not None and b["type"] == "openai" and b["api_key"] == "sk-from-env"

    def _run_with_responses(self, responses, base_url=None, api_key="sk-x"):
        """Drive _run_openai_loop with a fake OpenAI client returning `responses`
        (one per round)."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = responses
        with patch("openai.OpenAI", return_value=fake_client) as ctor:
            out = llm_agent._run_openai_loop(
                {"model": "gpt-4o", "api_key": api_key, "base_url": base_url},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )
        return out, ctor

    @staticmethod
    def _resp(content=None, tool_calls=None):
        msg = MagicMock()
        msg.content = content
        msg.tool_calls = tool_calls
        choice = MagicMock()
        choice.message = msg
        r = MagicMock()
        r.choices = [choice]
        return r

    def test_text_only_response(self):
        out, _ = self._run_with_responses([self._resp(content="All healthy.")])
        assert out == "All healthy."

    def test_empty_choices_does_not_crash(self):
        r = MagicMock()
        r.choices = []
        out, _ = self._run_with_responses([r])
        assert out == ""

    def test_tool_call_then_final(self, monkeypatch):
        # First round asks for a tool; second round returns text.
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "get_lock_graph"
        tc.function.arguments = "{}"
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        out, _ = self._run_with_responses([
            self._resp(content=None, tool_calls=[tc]),
            self._resp(content="Done: no locks."),
        ])
        assert out == "Done: no locks."

    def test_base_url_without_key_gets_placeholder(self):
        _, ctor = self._run_with_responses(
            [self._resp(content="ok")],
            base_url="http://localhost:11434/v1", api_key=None,
        )
        # base_url passed; SDK constructed with a non-empty placeholder key so a
        # keyless OpenAI-compatible server (Ollama, vLLM) doesn't trip the SDK.
        kwargs = ctor.call_args.kwargs
        assert kwargs.get("base_url") == "http://localhost:11434/v1"
        assert kwargs.get("api_key") == "not-needed"


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
