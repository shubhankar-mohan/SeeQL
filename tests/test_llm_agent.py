"""Tests for the LLM agent provider layer (agent/llm_agent.py).

These guard the bug class that kept shipping undetected: provider selection,
SDK response-shape parsing, and section extraction. Before this file, the
provider loops and `_detect_backend` had zero direct coverage — the higher-level
tests all mocked `run_llm_analysis` wholesale.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.responses import JSONResponse

from agent import llm_agent
from api import agent_routes


def test_shipped_defaults_are_current():
    """The config we ship must match what the README promises (agent is
    opt-in) and what's actually deployable on Vertex AI today."""
    import yaml
    cfg = yaml.safe_load(open("config/settings.yaml"))
    assert cfg["agent"]["enabled"] is False          # opt-in, as README promises
    assert cfg["agent"]["model"] == "gemini-2.5-flash"  # 2.0-flash is retired on Vertex


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
        b = llm_agent._detect_backend({"model": "gemini-2.5-flash"})
        assert b is not None
        assert b["type"] == "gemini"
        assert b["model"] == "gemini-2.5-flash"

    def test_gcp_creds_via_adc(self, monkeypatch):
        """Plain `gcloud auth application-default login` machines (no
        GOOGLE_APPLICATION_CREDENTIALS env var) must still select a Vertex
        backend when google-auth can resolve ADC."""
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {"project_id": "x"}})
        monkeypatch.setattr(llm_agent, "_adc_available", lambda: True)
        b = llm_agent._detect_backend({"model": "gemini-2.5-flash"})
        assert b is not None and b["type"] == "gemini"

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
        assert b["model"] == "gemini-2.5-flash"
        assert any("no matching backend" in r.message for r in caplog.records)

    def test_no_creds_returns_none(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {}})
        # Unsubstituted ${...} placeholder must not count as a real key.
        b = llm_agent._detect_backend(
            {"model": "claude-x", "anthropic_api_key": "${ANTHROPIC_API_KEY}"}
        )
        assert b is None

    def test_claude_with_both_gcp_and_api_key_prefers_anthropic(self, monkeypatch, caplog):
        """P1-28: when a claude-* model has BOTH GCP credentials and an
        explicit ANTHROPIC_API_KEY configured, prefer the Anthropic API — an
        explicit key is a deliberate choice, and it must be logged which
        backend was chosen."""
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/fake.json")
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"gcp": {"project_id": "p"}})
        with caplog.at_level(logging.INFO):
            b = llm_agent._detect_backend(
                {"model": "claude-opus-4-6", "anthropic_api_key": "sk-real-key"}
            )
        assert b is not None
        assert b["type"] == "anthropic"
        assert b["model"] == "claude-opus-4-6"
        assert any("preferring the Anthropic API" in r.message for r in caplog.records)

    def test_placeholder_project_id_does_not_hijack_anthropic_via_adc(self, monkeypatch):
        """The shipped `gcp.project_id: your-gcp-project-id` placeholder must not
        count as a real project id. A user who opts into a claude-* model with
        ANTHROPIC_API_KEY, leaves gcp.project_id untouched, and happens to run
        on a machine with ambient gcloud ADC must still get the Anthropic API
        backend — not vertex-claude with a bogus project id that fails at
        runtime."""
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setattr(
            llm_agent, "get_config",
            lambda: {"gcp": {"project_id": "your-gcp-project-id"}},
        )
        monkeypatch.setattr(llm_agent, "_adc_available", lambda: True)
        b = llm_agent._detect_backend(
            {"model": "claude-opus-4-6", "anthropic_api_key": "sk-test"}
        )
        assert b is not None
        assert b["type"] == "anthropic"


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

    def _run_with_responses(self, responses, base_url=None, api_key="sk-x", model="gpt-4o"):
        """Drive _run_openai_loop with a fake OpenAI client returning `responses`
        (one per round). Returns (text, truncated, ctor) — _run_openai_loop
        itself returns (text, truncated, tool_calls) since P1-22."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = responses
        with patch("openai.OpenAI", return_value=fake_client) as ctor:
            text, truncated, _tool_calls = llm_agent._run_openai_loop(
                {"model": model, "api_key": api_key, "base_url": base_url},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )
        return text, truncated, ctor

    @staticmethod
    def _resp(content=None, tool_calls=None, finish_reason="stop"):
        msg = MagicMock()
        msg.content = content
        msg.tool_calls = tool_calls
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = finish_reason
        r = MagicMock()
        r.choices = [choice]
        return r

    def test_text_only_response(self):
        out, truncated, _ = self._run_with_responses([self._resp(content="All healthy.")])
        assert out == "All healthy."
        assert truncated is False

    def test_empty_choices_does_not_crash(self):
        r = MagicMock()
        r.choices = []
        out, _, _ = self._run_with_responses([r])
        assert out == ""

    def test_tool_call_then_final(self, monkeypatch):
        # First round asks for a tool; second round returns text.
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "get_lock_graph"
        tc.function.arguments = "{}"
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        out, _, _ = self._run_with_responses([
            self._resp(content=None, tool_calls=[tc]),
            self._resp(content="Done: no locks."),
        ])
        assert out == "Done: no locks."

    def test_base_url_without_key_gets_placeholder(self):
        _, _, ctor = self._run_with_responses(
            [self._resp(content="ok")],
            base_url="http://localhost:11434/v1", api_key=None,
        )
        # base_url passed; SDK constructed with a non-empty placeholder key so a
        # keyless OpenAI-compatible server (Ollama, vLLM) doesn't trip the SDK.
        kwargs = ctor.call_args.kwargs
        assert kwargs.get("base_url") == "http://localhost:11434/v1"
        assert kwargs.get("api_key") == "not-needed"

    def test_truncated_finish_reason_length_sets_flag(self, caplog):
        """P1-13: finish_reason='length' must be surfaced, not swallowed."""
        with caplog.at_level(logging.WARNING):
            out, truncated, _ = self._run_with_responses(
                [self._resp(content="cut off mid-", finish_reason="length")]
            )
        assert out == "cut off mid-"
        assert truncated is True
        assert any("truncated" in r.message.lower() for r in caplog.records)

    def test_o_series_model_sends_max_completion_tokens_no_temperature(self):
        """P1-11: o-series (o1/o3/o4-*) reject `max_tokens` + `temperature`;
        they take `max_completion_tokens` instead."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [self._resp(content="ok")]
        with patch("openai.OpenAI", return_value=fake_client):
            llm_agent._run_openai_loop(
                {"model": "o3-mini", "api_key": "sk-x", "base_url": None},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )
        kwargs = fake_client.chat.completions.create.call_args.kwargs
        assert kwargs.get("max_completion_tokens") == 100
        assert "temperature" not in kwargs
        assert "max_tokens" not in kwargs

    def test_non_o_series_model_still_sends_max_tokens_and_temperature(self):
        """Regression guard alongside the o-series carve-out above."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [self._resp(content="ok")]
        with patch("openai.OpenAI", return_value=fake_client):
            llm_agent._run_openai_loop(
                {"model": "gpt-4o", "api_key": "sk-x", "base_url": None},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )
        kwargs = fake_client.chat.completions.create.call_args.kwargs
        assert kwargs.get("max_tokens") == 100
        assert kwargs.get("temperature") == 0
        assert "max_completion_tokens" not in kwargs

    def test_finalize_call_on_max_rounds_exhaustion(self, monkeypatch):
        """P1-5: still tool-calling at max_rounds -> exactly one extra
        finalize create() with tools omitted, and its text is returned."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "get_lock_graph"
        tc.function.arguments = "{}"
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")

        # max_rounds=2: both rounds keep calling tools, never break naturally.
        keeps_calling = self._resp(content=None, tool_calls=[tc])
        finalize_text = self._resp(content="Final: locks cleared.")
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [
            keeps_calling, keeps_calling, finalize_text,
        ]
        with patch("openai.OpenAI", return_value=fake_client):
            text, truncated, tool_calls = llm_agent._run_openai_loop(
                {"model": "gpt-4o", "api_key": "sk-x", "base_url": None},
                max_tokens=100, max_rounds=2, user_msg="hi",
            )

        assert text == "Final: locks cleared."
        assert tool_calls == 2
        assert fake_client.chat.completions.create.call_count == 3
        finalize_kwargs = fake_client.chat.completions.create.call_args_list[2].kwargs
        assert "tools" not in finalize_kwargs
        assert finalize_kwargs["messages"][-1] == {
            "role": "user",
            "content": llm_agent._FINALIZE_NUDGE,
        }

    def test_finalize_empty_text_returns_empty_not_stale(self, monkeypatch):
        """P1-5: if the finalize call also comes back empty, don't fall back
        to stale text from an earlier round — return "" so the caller skips
        storing (and doesn't link an incident to an empty analysis)."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "get_lock_graph"
        tc.function.arguments = "{}"
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")

        # Round 1 leaves stale text AND keeps calling tools; the finalize
        # response then comes back with no content at all.
        stale_then_calls = self._resp(content="stale partial thought", tool_calls=[tc])
        empty_finalize = self._resp(content=None)
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [stale_then_calls, empty_finalize]
        with patch("openai.OpenAI", return_value=fake_client):
            text, _, _ = llm_agent._run_openai_loop(
                {"model": "gpt-4o", "api_key": "sk-x", "base_url": None},
                max_tokens=100, max_rounds=1, user_msg="hi",
            )

        assert text == ""

    def test_custom_system_prompt_used_in_messages(self):
        """P3-2: a system_prompt passed in must be what's actually sent —
        not the bare SYSTEM_PROMPT constant."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [self._resp(content="ok")]
        with patch("openai.OpenAI", return_value=fake_client):
            llm_agent._run_openai_loop(
                {"model": "gpt-4o", "api_key": "sk-x", "base_url": None},
                max_tokens=100, max_rounds=3, user_msg="hi",
                system_prompt="CUSTOM VERSIONED PROMPT",
            )
        kwargs = fake_client.chat.completions.create.call_args.kwargs
        assert kwargs["messages"][0] == {
            "role": "system", "content": "CUSTOM VERSIONED PROMPT",
        }

    def test_default_system_prompt_is_the_bare_constant(self):
        """Regression guard: omitting system_prompt must still behave like
        before P3-2 (direct callers/tests don't have to supply one)."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = [self._resp(content="ok")]
        with patch("openai.OpenAI", return_value=fake_client):
            llm_agent._run_openai_loop(
                {"model": "gpt-4o", "api_key": "sk-x", "base_url": None},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )
        kwargs = fake_client.chat.completions.create.call_args.kwargs
        assert kwargs["messages"][0] == {
            "role": "system", "content": llm_agent.SYSTEM_PROMPT,
        }


class TestGeminiResponseShapes:
    """Gemini can return empty/None candidates (safety/recitation blocks,
    MAX_TOKENS with no content) — the tool loop must not IndexError."""

    def _run_with_response(self, fake_response, max_rounds=3):
        """Returns (text, truncated, tool_calls) — _run_gemini_loop's contract since P1-22."""
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response
        with patch("google.genai.Client", return_value=fake_client):
            return llm_agent._run_gemini_loop(
                {"model": "gemini-2.5-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100,
                max_rounds=max_rounds,
                user_msg="hi",
            )

    @staticmethod
    def _candidate(text=None, function_call=None, finish_reason=None):
        part = MagicMock()
        part.text = text
        part.function_call = function_call
        content = MagicMock()
        content.parts = [part]
        cand = MagicMock()
        cand.content = content
        cand.finish_reason = finish_reason
        return cand

    def test_empty_candidates_does_not_crash(self):
        resp = MagicMock()
        resp.candidates = []
        assert self._run_with_response(resp) == ("", False, 0)

    def test_none_candidates_does_not_crash(self):
        resp = MagicMock()
        resp.candidates = None
        assert self._run_with_response(resp) == ("", False, 0)

    def test_text_only_response_returns_text(self):
        resp = MagicMock()
        resp.candidates = [self._candidate(text="All healthy.")]
        text, truncated, _ = self._run_with_response(resp)
        assert text == "All healthy."
        assert truncated is False

    def test_truncated_finish_reason_sets_flag(self, caplog):
        """P1-13: Gemini's MAX_TOKENS finish_reason must be surfaced."""
        resp = MagicMock()
        resp.candidates = [self._candidate(text="cut off", finish_reason="MAX_TOKENS")]
        with caplog.at_level(logging.WARNING):
            text, truncated, _ = self._run_with_response(resp)
        assert text == "cut off"
        assert truncated is True
        assert any("truncated" in r.message.lower() for r in caplog.records)

    def test_function_call_id_threaded_into_response_part(self, monkeypatch):
        """P1-12: parallel same-tool calls in one turn must be keyed by id,
        not just name, or results can get crossed."""
        from google.genai import types as genai_types

        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")

        fc = MagicMock()
        fc.name = "get_lock_graph"
        fc.args = {}
        fc.id = "call_abc"
        round1 = MagicMock()
        round1.candidates = [self._candidate(function_call=fc)]
        round2 = MagicMock()
        round2.candidates = [self._candidate(text="Done.")]

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [round1, round2]
        with patch("google.genai.Client", return_value=fake_client):
            text, _, _ = llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )

        assert text == "Done."
        # Second call's `contents` includes the tool-response Part; assert it
        # carries the function call's id through to the FunctionResponse.
        second_call_contents = fake_client.models.generate_content.call_args_list[1].kwargs[
            "contents"
        ]
        tool_response_content = second_call_contents[-1]
        response_part = tool_response_content.parts[0]
        assert isinstance(response_part, genai_types.Part)
        assert response_part.function_response.id == "call_abc"
        assert response_part.function_response.name == "get_lock_graph"

    def test_function_call_without_id_leaves_id_none(self, monkeypatch):
        """No id on the function call (older SDK/provider shape) -> the
        response Part carries id=None (serializes the same as omitting it)."""
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")

        fc = MagicMock()
        fc.name = "get_lock_graph"
        fc.args = {}
        fc.id = None
        round1 = MagicMock()
        round1.candidates = [self._candidate(function_call=fc)]
        round2 = MagicMock()
        round2.candidates = [self._candidate(text="Done.")]

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [round1, round2]
        with patch("google.genai.Client", return_value=fake_client):
            text, _, _ = llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )

        assert text == "Done."
        second_call_contents = fake_client.models.generate_content.call_args_list[1].kwargs[
            "contents"
        ]
        response_part = second_call_contents[-1].parts[0]
        assert response_part.function_response.id is None
        assert response_part.function_response.name == "get_lock_graph"

    def test_finalize_call_on_max_rounds_exhaustion(self, monkeypatch):
        """P1-5: still tool-calling at max_rounds -> exactly one extra
        finalize generate_content() with tools omitted, text returned."""
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        fc = MagicMock()
        fc.name = "get_lock_graph"
        fc.args = {}
        fc.id = "call_1"

        keeps_calling = MagicMock()
        keeps_calling.candidates = [self._candidate(function_call=fc)]
        finalize = MagicMock()
        finalize.candidates = [self._candidate(text="Final: locks cleared.")]

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [
            keeps_calling, keeps_calling, finalize,
        ]
        with patch("google.genai.Client", return_value=fake_client):
            text, truncated, tool_calls = llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100, max_rounds=2, user_msg="hi",
            )

        assert text == "Final: locks cleared."
        assert truncated is False
        assert tool_calls == 2
        assert fake_client.models.generate_content.call_count == 3
        finalize_kwargs = fake_client.models.generate_content.call_args_list[2].kwargs
        assert finalize_kwargs["config"].tools is None
        last_content = finalize_kwargs["contents"][-1]
        assert last_content.parts[0].text == llm_agent._FINALIZE_NUDGE

    def test_finalize_empty_text_returns_empty_not_stale(self, monkeypatch):
        """P1-5: an empty finalize response returns "" rather than falling
        back to stale text from an earlier round."""
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        fc = MagicMock()
        fc.name = "get_lock_graph"
        fc.args = {}
        fc.id = "call_1"

        stale_then_calls = MagicMock()
        stale_then_calls.candidates = [
            self._candidate(text="stale partial thought", function_call=fc)
        ]
        empty_finalize = MagicMock()
        empty_finalize.candidates = []

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [stale_then_calls, empty_finalize]
        with patch("google.genai.Client", return_value=fake_client):
            text, _, _ = llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100, max_rounds=1, user_msg="hi",
            )

        assert text == ""

    def test_custom_system_prompt_passed_to_system_instruction(self):
        """P3-2: a system_prompt passed in must be what's actually sent —
        not the bare SYSTEM_PROMPT constant."""
        resp = MagicMock()
        resp.candidates = [self._candidate(text="ok")]
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = resp
        with patch("google.genai.Client", return_value=fake_client):
            llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100, max_rounds=3, user_msg="hi",
                system_prompt="CUSTOM VERSIONED PROMPT",
            )
        kwargs = fake_client.models.generate_content.call_args.kwargs
        assert kwargs["config"].system_instruction == "CUSTOM VERSIONED PROMPT"

    def test_default_system_prompt_is_the_bare_constant(self):
        """Regression guard: omitting system_prompt must still behave like
        before P3-2 (direct callers/tests don't have to supply one)."""
        resp = MagicMock()
        resp.candidates = [self._candidate(text="ok")]
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = resp
        with patch("google.genai.Client", return_value=fake_client):
            llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )
        kwargs = fake_client.models.generate_content.call_args.kwargs
        assert kwargs["config"].system_instruction == llm_agent.SYSTEM_PROMPT

    def test_tool_result_passed_as_parsed_object_not_json_string(self, monkeypatch):
        """P1-24: execute_tool() returns a JSON string; Gemini must get the
        PARSED object in response={"result": ...}, not a string it has to
        re-parse itself on every tool result."""
        monkeypatch.setattr(
            llm_agent, "execute_tool",
            lambda name, args: json.dumps({"rows_examined": 500000, "rows_sent": 3}),
        )
        fc = MagicMock()
        fc.name = "run_explain"
        fc.args = {}
        fc.id = "call_1"
        round1 = MagicMock()
        round1.candidates = [self._candidate(function_call=fc)]
        round2 = MagicMock()
        round2.candidates = [self._candidate(text="Done.")]

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [round1, round2]
        with patch("google.genai.Client", return_value=fake_client):
            llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )

        second_call_contents = fake_client.models.generate_content.call_args_list[1].kwargs[
            "contents"
        ]
        response_part = second_call_contents[-1].parts[0]
        assert response_part.function_response.response == {
            "result": {"rows_examined": 500000, "rows_sent": 3}
        }

    def test_tool_result_falls_back_to_raw_string_when_not_json(self, monkeypatch):
        """P1-24: a handler that doesn't return valid JSON (e.g. a plain
        status string) must still be passed through, not crash the loop."""
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        fc = MagicMock()
        fc.name = "get_lock_graph"
        fc.args = {}
        fc.id = "call_1"
        round1 = MagicMock()
        round1.candidates = [self._candidate(function_call=fc)]
        round2 = MagicMock()
        round2.candidates = [self._candidate(text="Done.")]

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [round1, round2]
        with patch("google.genai.Client", return_value=fake_client):
            llm_agent._run_gemini_loop(
                {"model": "gemini-2.0-flash", "project_id": "p", "region": "us-central1"},
                max_tokens=100, max_rounds=3, user_msg="hi",
            )

        second_call_contents = fake_client.models.generate_content.call_args_list[1].kwargs[
            "contents"
        ]
        response_part = second_call_contents[-1].parts[0]
        assert response_part.function_response.response == {"result": "no locks"}


class TestCreateWithRetry:
    """P1-6: _create_with_retry wraps every provider create() call with
    bounded retry + backoff on transient errors."""

    def test_succeeds_after_retryable_failures(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(llm_agent.time, "sleep", lambda s: sleeps.append(s))
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("529 overloaded")
            return "ok"

        result = llm_agent._create_with_retry(fn)

        assert result == "ok"
        assert calls["n"] == 3
        assert sleeps == [2, 8]

    def test_non_retryable_raises_immediately_without_sleeping(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(llm_agent.time, "sleep", lambda s: sleeps.append(s))
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise Exception("400 bad request: invalid schema")

        with pytest.raises(Exception, match="400 bad request"):
            llm_agent._create_with_retry(fn)

        assert calls["n"] == 1
        assert sleeps == []

    def test_retryable_error_still_raises_once_attempts_exhausted(self, monkeypatch):
        monkeypatch.setattr(llm_agent.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise Exception("rate limit exceeded, please retry")

        with pytest.raises(Exception, match="rate limit"):
            llm_agent._create_with_retry(fn, attempts=3)

        assert calls["n"] == 3

    def test_custom_attempts_respected(self, monkeypatch):
        monkeypatch.setattr(llm_agent.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise Exception("timeout while waiting for response")

        with pytest.raises(Exception):
            llm_agent._create_with_retry(fn, attempts=1)

        assert calls["n"] == 1


class TestClaudeLoop:
    """Direct client-mock tests for _run_claude_loop — the Claude backend
    (default fallback when GCP creds are absent) had zero direct loop tests
    before this file (P4-16); this ports the OpenAI mock pattern above."""

    def _run_with_responses(self, responses, model="claude-x", max_rounds=3):
        """Returns (text, truncated, client). _run_claude_loop's own return
        contract is (text, truncated, tool_calls) since P1-22."""
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = responses
        text, truncated, _tool_calls = llm_agent._run_claude_loop(
            fake_client, model, max_tokens=100, max_rounds=max_rounds, user_msg="hi",
        )
        return text, truncated, fake_client

    @staticmethod
    def _text_block(text):
        b = MagicMock()
        b.type = "text"
        b.text = text
        return b

    @staticmethod
    def _tool_use_block(name, input_=None, id_="tool_1"):
        b = MagicMock()
        b.type = "tool_use"
        b.name = name
        b.input = input_ or {}
        b.id = id_
        return b

    @classmethod
    def _resp(cls, blocks, stop_reason="end_turn"):
        r = MagicMock()
        r.content = blocks
        r.stop_reason = stop_reason
        return r

    def test_text_only_response(self):
        out, truncated, _ = self._run_with_responses(
            [self._resp([self._text_block("All healthy.")])]
        )
        assert out == "All healthy."
        assert truncated is False

    def test_tool_call_then_final(self, monkeypatch):
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        out, _, client = self._run_with_responses([
            self._resp([self._tool_use_block("get_lock_graph")], stop_reason="tool_use"),
            self._resp([self._text_block("Done: no locks.")]),
        ])
        assert out == "Done: no locks."
        assert client.messages.create.call_count == 2

    def test_retry_on_overloaded_then_succeeds(self, monkeypatch):
        """529 twice then a real response -> the analysis completes; the
        completed tool rounds already in `messages` aren't discarded."""
        monkeypatch.setattr(llm_agent.time, "sleep", lambda s: None)
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            Exception("529 overloaded"),
            Exception("529 overloaded"),
            self._resp([self._text_block("Recovered.")]),
        ]

        text, _, _ = llm_agent._run_claude_loop(
            fake_client, "claude-x", max_tokens=100, max_rounds=3, user_msg="hi",
        )

        assert text == "Recovered."
        assert fake_client.messages.create.call_count == 3

    def test_non_retryable_error_after_two_tool_rounds_propagates(self, monkeypatch):
        """Mirrors the run_analysis-level contract (P1-6): two completed
        tool rounds, then a non-retryable error -> the exception propagates
        out of the loop so the caller can report an honest failure."""
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._resp([self._tool_use_block("get_lock_graph", id_="t1")], stop_reason="tool_use"),
            self._resp([self._tool_use_block("get_lock_graph", id_="t2")], stop_reason="tool_use"),
            Exception("400 bad request: invalid schema"),
        ]

        with pytest.raises(Exception, match="400 bad request"):
            llm_agent._run_claude_loop(
                fake_client, "claude-x", max_tokens=100, max_rounds=5, user_msg="hi",
            )

        assert fake_client.messages.create.call_count == 3

    def test_is_error_flag_set_when_tool_result_is_json_error(self, monkeypatch):
        """P1-26: execute_tool() serializes failures as `{"error": ...}` —
        that must be flagged is_error=True so Claude treats it as a failed
        call rather than legitimate data."""
        monkeypatch.setattr(
            llm_agent, "execute_tool",
            lambda name, args: json.dumps({"error": "Unknown tool: bogus"}),
        )
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._resp([self._tool_use_block("bogus", id_="t1")], stop_reason="tool_use"),
            self._resp([self._text_block("done")]),
        ]

        llm_agent._run_claude_loop(
            fake_client, "claude-x", max_tokens=100, max_rounds=3, user_msg="hi",
        )

        second_call_messages = fake_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result_block = second_call_messages[-1]["content"][0]
        assert tool_result_block["is_error"] is True

    def test_non_error_tool_result_has_no_is_error_flag(self, monkeypatch):
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._resp([self._tool_use_block("get_lock_graph", id_="t1")], stop_reason="tool_use"),
            self._resp([self._text_block("done")]),
        ]

        llm_agent._run_claude_loop(
            fake_client, "claude-x", max_tokens=100, max_rounds=3, user_msg="hi",
        )

        second_call_messages = fake_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result_block = second_call_messages[-1]["content"][0]
        assert "is_error" not in tool_result_block

    def test_truncated_stop_reason_max_tokens_sets_flag(self, caplog):
        with caplog.at_level(logging.WARNING):
            _, truncated, _ = self._run_with_responses([
                self._resp([self._text_block("cut off mid-")], stop_reason="max_tokens"),
            ])
        assert truncated is True
        assert any("truncated" in r.message.lower() for r in caplog.records)

    def test_finalize_call_on_max_rounds_exhaustion(self, monkeypatch):
        """P1-5: still tool-calling at max_rounds -> exactly one extra
        finalize create() with tool_choice=none, and its text is returned."""
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        keeps_calling = self._resp(
            [self._tool_use_block("get_lock_graph", id_="t1")], stop_reason="tool_use"
        )
        finalize_resp = self._resp([self._text_block("Final: locks cleared.")])
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [keeps_calling, keeps_calling, finalize_resp]

        text, truncated, tool_calls = llm_agent._run_claude_loop(
            fake_client, "claude-x", max_tokens=100, max_rounds=2, user_msg="hi",
        )

        assert text == "Final: locks cleared."
        assert truncated is False
        assert tool_calls == 2
        assert fake_client.messages.create.call_count == 3
        finalize_kwargs = fake_client.messages.create.call_args_list[2].kwargs
        assert finalize_kwargs["tool_choice"] == {"type": "none"}
        assert finalize_kwargs["messages"][-1] == {
            "role": "user", "content": llm_agent._FINALIZE_NUDGE,
        }

    def test_finalize_empty_text_returns_empty_not_stale(self, monkeypatch):
        """P1-5: an empty finalize response returns "" rather than falling
        back to stale text left over from an earlier round."""
        monkeypatch.setattr(llm_agent, "execute_tool", lambda name, args: "no locks")
        stale_then_calls = self._resp(
            [self._text_block("stale partial thought"),
             self._tool_use_block("get_lock_graph", id_="t1")],
            stop_reason="tool_use",
        )
        empty_finalize = self._resp([])
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [stale_then_calls, empty_finalize]

        text, _, _ = llm_agent._run_claude_loop(
            fake_client, "claude-x", max_tokens=100, max_rounds=1, user_msg="hi",
        )

        assert text == ""

    def test_custom_system_prompt_used_in_system_kwarg(self):
        """P3-2: a system_prompt passed in must be what's actually sent —
        not the bare SYSTEM_PROMPT constant."""
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [self._resp([self._text_block("ok")])]
        llm_agent._run_claude_loop(
            fake_client, "claude-x", max_tokens=100, max_rounds=3, user_msg="hi",
            system_prompt="CUSTOM VERSIONED PROMPT",
        )
        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs["system"] == "CUSTOM VERSIONED PROMPT"

    def test_default_system_prompt_is_the_bare_constant(self):
        """Regression guard: omitting system_prompt must still behave like
        before P3-2 (direct callers/tests don't have to supply one)."""
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [self._resp([self._text_block("ok")])]
        llm_agent._run_claude_loop(
            fake_client, "claude-x", max_tokens=100, max_rounds=3, user_msg="hi",
        )
        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs["system"] == llm_agent.SYSTEM_PROMPT


class TestParseAndStoreEmptyText:
    """P1-5: an empty/whitespace-only response must not be stored, and must
    not link (or leave "detected") any incident."""

    def test_empty_text_is_not_stored(self, monkeypatch):
        mock_write = MagicMock(return_value=(999, True))
        monkeypatch.setattr(llm_agent.writer, "write_agent_analysis_and_link", mock_write)

        result = llm_agent._parse_and_store("", "incident", "summary", "default", incident_id=7)

        assert result["stored"] is False
        assert result["id"] is None
        mock_write.assert_not_called()

    def test_whitespace_only_text_is_not_stored(self, monkeypatch):
        mock_write = MagicMock(return_value=(999, True))
        monkeypatch.setattr(llm_agent.writer, "write_agent_analysis_and_link", mock_write)

        result = llm_agent._parse_and_store(
            "   \n  ", "incident", "summary", "default", incident_id=7
        )

        assert result["stored"] is False
        mock_write.assert_not_called()

    def test_empty_text_logs_error(self, caplog, monkeypatch):
        mock_write = MagicMock(return_value=(1, True))
        monkeypatch.setattr(llm_agent.writer, "write_agent_analysis_and_link", mock_write)

        with caplog.at_level(logging.ERROR):
            llm_agent._parse_and_store("", "routine", "summary", "default")

        assert any("empty" in r.message.lower() for r in caplog.records)

    def test_empty_text_leaves_real_incident_detected(self, mon_db, test_config):
        """End-to-end (real SQLite, not mocked): an incident row stays
        'detected' — not linked/analyzed, no agent_analyses row at all —
        when the response text is empty."""
        import config as config_module
        conn, db_path = mon_db
        # mon_db resets config back to None after running its migrations, so
        # point the app at the same tmp_path db test_config already computed
        # (both fixtures share the test's tmp_path) before exercising the
        # write path.
        assert str(db_path) == test_config["monitoring_db"]["path"]
        config_module._config = test_config
        cur = conn.execute(
            "INSERT INTO incident_windows (server_id, start_time, end_time, "
            "severity, involved_metrics, status) VALUES "
            "('default', '2026-07-17T00:00:00', '2026-07-17T00:05:00', "
            "'critical', '[\"x\"]', 'detected')"
        )
        conn.commit()
        iid = cur.lastrowid

        result = llm_agent._parse_and_store(
            "", "incident", "summary", "default", incident_id=iid,
        )

        assert result["stored"] is False
        row = conn.execute(
            "SELECT status, analysis_id FROM incident_windows WHERE id = ?", (iid,)
        ).fetchone()
        assert row["status"] == "detected"
        assert row["analysis_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM agent_analyses").fetchone()[0] == 0

    def test_nonempty_text_still_stores_and_links_incident(self, mon_db, test_config):
        """Regression guard alongside the empty-text skip above: a real
        response still stores normally and can still link an incident."""
        import config as config_module
        conn, db_path = mon_db
        assert str(db_path) == test_config["monitoring_db"]["path"]
        config_module._config = test_config
        cur = conn.execute(
            "INSERT INTO incident_windows (server_id, start_time, end_time, "
            "severity, involved_metrics, status) VALUES "
            "('default', '2026-07-17T00:00:00', '2026-07-17T00:05:00', "
            "'critical', '[\"x\"]', 'detected')"
        )
        conn.commit()
        iid = cur.lastrowid

        text = "### Severity: info\n### Findings\nx\n### Recommendations\ny\n"
        result = llm_agent._parse_and_store(text, "incident", "summary", "default", incident_id=iid)

        assert result["stored"] is True
        assert result["id"] is not None
        row = conn.execute(
            "SELECT status, analysis_id FROM incident_windows WHERE id = ?", (iid,)
        ).fetchone()
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == result["id"]


class TestRunLlmAnalysisEmptyText:
    """P1-5, sibling path: run_llm_analysis (used by replay + the webhook
    investigator, both of which pass incident_id explicitly) must apply the
    same empty-response guard as _parse_and_store — never persist a
    content-free analysis or link/close an incident against one."""

    def _wire_backend(self, monkeypatch, response_text):
        monkeypatch.setattr(
            llm_agent, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )
        # _run_anthropic_loop returns (text, truncated, tool_calls) since P1-22,
        # and now takes a 5th system_prompt arg (P3-2).
        monkeypatch.setattr(
            llm_agent, "_run_anthropic_loop",
            lambda backend, max_tokens, max_rounds, prompt, system_prompt=None: (
                response_text, False, 1,
            ),
        )

    def test_empty_text_does_not_store_or_link(self, mon_db, test_config, monkeypatch):
        import config as config_module
        conn, db_path = mon_db
        assert str(db_path) == test_config["monitoring_db"]["path"]
        config_module._config = {**test_config, "agent": {}}
        cur = conn.execute(
            "INSERT INTO incident_windows (server_id, start_time, end_time, "
            "severity, involved_metrics, status) VALUES "
            "('default', '2026-07-17T00:00:00', '2026-07-17T00:05:00', "
            "'critical', '[\"x\"]', 'detected')"
        )
        conn.commit()
        iid = cur.lastrowid

        self._wire_backend(monkeypatch, "")
        result = llm_agent.run_llm_analysis(
            "replay prompt", analysis_type="replay", server_id="default", incident_id=iid,
        )

        assert result["analysis_id"] is None
        row = conn.execute(
            "SELECT status, analysis_id FROM incident_windows WHERE id = ?", (iid,)
        ).fetchone()
        assert row["status"] == "detected"          # not resurrected/analyzed
        assert row["analysis_id"] is None
        assert conn.execute("SELECT COUNT(*) FROM agent_analyses").fetchone()[0] == 0

    def test_nonempty_text_still_stores_and_links(self, mon_db, test_config, monkeypatch):
        """Regression guard: a real response still stores + links normally."""
        import config as config_module
        conn, db_path = mon_db
        config_module._config = {**test_config, "agent": {}}
        cur = conn.execute(
            "INSERT INTO incident_windows (server_id, start_time, end_time, "
            "severity, involved_metrics, status) VALUES "
            "('default', '2026-07-17T00:00:00', '2026-07-17T00:05:00', "
            "'critical', '[\"x\"]', 'detected')"
        )
        conn.commit()
        iid = cur.lastrowid

        self._wire_backend(
            monkeypatch, "### Severity: info\n### Findings\nx\n### Recommendations\ny\n"
        )
        result = llm_agent.run_llm_analysis(
            "replay prompt", analysis_type="replay", server_id="default", incident_id=iid,
        )

        assert result["analysis_id"] is not None
        row = conn.execute(
            "SELECT status, analysis_id FROM incident_windows WHERE id = ?", (iid,)
        ).fetchone()
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == result["analysis_id"]


class TestRunAnalysisHonestErrors:
    """P1-6: a loop exception must be distinguishable from a quiet skip
    (None), and the API route must map it to a 502 — not "skipped"."""

    @staticmethod
    def _fake_report():
        report = MagicMock()
        report.to_markdown.return_value = "## fake state report"
        return report

    def _wire_backend(self, monkeypatch, loop_fn):
        monkeypatch.setattr(
            llm_agent, "get_config",
            lambda: {"agent": {"enabled": True, "model": "claude-x"}},
        )
        monkeypatch.setattr(
            llm_agent, "build_state_report",
            lambda server_id=None: self._fake_report(),
        )
        # build_system_prompt would otherwise hit the real monitoring DB and
        # real on-disk config (this class doesn't wire up a mon_db fixture) —
        # stub it out the same way build_state_report is stubbed above (P3-2).
        monkeypatch.setattr(
            llm_agent, "build_system_prompt",
            lambda server_id=None: "stub system prompt",
        )
        monkeypatch.setattr(
            llm_agent, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )
        monkeypatch.setattr(llm_agent, "_run_anthropic_loop", loop_fn)

    def test_loop_exception_returns_error_shape(self, monkeypatch):
        def boom(backend, max_tokens, max_rounds, user_msg, system_prompt=None):
            raise Exception("400 bad request: invalid schema")

        self._wire_backend(monkeypatch, boom)

        result = llm_agent.run_analysis("incident", server_id="default")

        assert result is not None
        assert result["status"] == "error"
        assert "400 bad request" in result["error"]

    def test_retryable_error_that_never_recovers_also_returns_error_shape(self, monkeypatch):
        """A retryable-but-persistent failure (529 on every attempt) must
        still come back as an honest error, not a silent None."""
        monkeypatch.setattr(llm_agent.time, "sleep", lambda s: None)

        def always_overloaded(backend, max_tokens, max_rounds, user_msg, system_prompt=None):
            raise Exception("529 overloaded")

        self._wire_backend(monkeypatch, always_overloaded)

        result = llm_agent.run_analysis("incident", server_id="default")

        assert result["status"] == "error"
        assert "529" in result["error"]

    def test_quiet_skip_is_none_not_error_shape(self, monkeypatch):
        """The pre-existing skip path (agent disabled) must stay a plain
        None — distinguishable from the new {"status": "error"} shape."""
        monkeypatch.setattr(llm_agent, "get_config", lambda: {"agent": {"enabled": False}})

        result = llm_agent.run_analysis("routine", server_id="default")

        assert result is None

    def test_api_route_maps_error_shape_to_502(self, monkeypatch):
        monkeypatch.setattr(
            llm_agent, "run_analysis",
            lambda analysis_type, server_id=None, incident_id=None, trigger_type=None: {
                "status": "error", "error": "400 bad request: invalid schema",
            },
        )

        response = agent_routes.trigger_analysis(analysis_type="routine", server="default")

        assert isinstance(response, JSONResponse)
        assert response.status_code == 502
        body = json.loads(response.body)
        assert body["status"] == "error"
        assert "400 bad request" in body["error"]

    def test_api_route_maps_stored_false_to_no_analysis(self, monkeypatch):
        """P1-5: a 'ran but produced nothing usable' result must NOT be
        reported as a 200 'completed' with null severity/findings."""
        monkeypatch.setattr(
            llm_agent, "run_analysis",
            lambda analysis_type, server_id=None, incident_id=None, trigger_type=None: {
                "stored": False, "id": None, "severity": None,
                "findings": None, "recommendations": None, "truncated": False,
            },
        )

        response = agent_routes.trigger_analysis(analysis_type="routine", server="default")

        assert isinstance(response, JSONResponse)
        assert response.status_code == 502
        body = json.loads(response.body)
        assert body["status"] == "no_analysis"

    def test_api_route_quiet_skip_stays_200(self, monkeypatch):
        monkeypatch.setattr(
            llm_agent, "run_analysis",
            lambda analysis_type, server_id=None, incident_id=None, trigger_type=None: None,
        )

        response = agent_routes.trigger_analysis(analysis_type="routine", server="default")

        assert response == {"status": "skipped", "reason": "Agent disabled or state is quiet"}

    def test_api_route_success_stays_completed(self, monkeypatch):
        monkeypatch.setattr(
            llm_agent, "run_analysis",
            lambda analysis_type, server_id=None, incident_id=None, trigger_type=None: {
                "severity": "warning", "findings": "[]", "recommendations": "[]",
            },
        )

        response = agent_routes.trigger_analysis(analysis_type="routine", server="default")

        assert response["status"] == "completed"
        assert response["severity"] == "warning"
        # P1-23: findings/recommendations are decoded, not left as a raw
        # JSON-encoded string the client would have to json.loads() again.
        assert response["findings"] == []
        assert response["recommendations"] == []


class TestRunAnalysisContextVarHygiene:
    """P1-10: the target-server ContextVar must reset even when an
    exception is raised while building the state report. Before this fix,
    only the LLM-loop dispatch was wrapped in try/finally -- a
    build_state_report() failure (a bad snapshot query, a locked SQLite
    file, ...) skipped the reset entirely and left this worker thread's
    ContextVar pointed at server_id for whatever job APScheduler runs next
    on that (reused) thread."""

    def test_context_reset_when_state_report_build_raises(self, monkeypatch):
        monkeypatch.setattr(
            llm_agent, "get_config",
            lambda: {"agent": {"enabled": True, "model": "claude-x"}},
        )

        def _boom(server_id=None):
            raise RuntimeError("state report build exploded")

        monkeypatch.setattr(llm_agent, "build_state_report", _boom)

        from agent.tools import get_current_server

        with pytest.raises(RuntimeError):
            llm_agent.run_analysis("routine", server_id="server_x")

        assert get_current_server() is None


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


# ---------------------------------------------------------------------------
# P3-2: versioned, per-server system prompt
# ---------------------------------------------------------------------------
class TestBuildSystemPrompt:
    """build_system_prompt() formats SYSTEM_PROMPT with the real MySQL
    version + hosting platform for a server, instead of the old hardcoded
    "MySQL 8.0.43 on GCP Cloud SQL" regardless of what's actually running."""

    def test_uses_seeded_mysql_version(self, mon_db, test_config):
        import config as config_module
        conn, db_path = mon_db
        config_module._config = test_config
        conn.execute(
            "INSERT INTO global_variable_snapshots "
            "(snapshot_time, server_id, variable_name, variable_value) "
            "VALUES ('2026-07-17T00:00:00', 'default', 'version', '8.0.43-google')"
        )
        conn.commit()

        prompt = llm_agent.build_system_prompt("default")

        assert "MySQL 8.0.43-google" in prompt

    def test_falls_back_to_8_0_plus_when_no_snapshot(self, mon_db, test_config):
        import config as config_module
        conn, db_path = mon_db
        config_module._config = test_config

        prompt = llm_agent.build_system_prompt("default")

        assert "MySQL 8.0+" in prompt

    def test_uses_latest_version_snapshot_not_oldest(self, mon_db, test_config):
        import config as config_module
        conn, db_path = mon_db
        config_module._config = test_config
        conn.execute(
            "INSERT INTO global_variable_snapshots "
            "(snapshot_time, server_id, variable_name, variable_value) "
            "VALUES ('2026-07-01T00:00:00', 'default', 'version', '8.0.30')"
        )
        conn.execute(
            "INSERT INTO global_variable_snapshots "
            "(snapshot_time, server_id, variable_name, variable_value) "
            "VALUES ('2026-07-17T00:00:00', 'default', 'version', '8.0.43-google')"
        )
        conn.commit()

        prompt = llm_agent.build_system_prompt("default")

        assert "8.0.43-google" in prompt
        assert "8.0.30" not in prompt

    def test_scoped_to_server_id_does_not_leak_other_servers_version(
        self, mon_db, test_config
    ):
        """P1-1-style scoping: a snapshot from a DIFFERENT server_id must
        never leak into this server's prompt."""
        import config as config_module
        conn, db_path = mon_db
        config_module._config = test_config
        conn.execute(
            "INSERT INTO global_variable_snapshots "
            "(snapshot_time, server_id, variable_name, variable_value) "
            "VALUES ('2026-07-17T00:00:00', 'other-server', 'version', '5.7.44')"
        )
        conn.commit()

        prompt = llm_agent.build_system_prompt("default")

        assert "5.7.44" not in prompt
        assert "MySQL 8.0+" in prompt

    def test_gcp_project_id_configured_uses_cloud_sql_platform(self, mon_db, test_config):
        import config as config_module
        conn, db_path = mon_db
        config_module._config = {**test_config, "gcp": {"project_id": "real-project"}}

        prompt = llm_agent.build_system_prompt("default")

        assert "a managed Cloud SQL instance" in prompt

    def test_no_gcp_project_id_uses_self_hosted_platform(self, mon_db, test_config):
        import config as config_module
        conn, db_path = mon_db
        config_module._config = {**test_config, "gcp": {}}

        prompt = llm_agent.build_system_prompt("default")

        assert "a MySQL server (managed or self-hosted)" in prompt

    def test_placeholder_gcp_project_id_uses_self_hosted_platform(self, mon_db, test_config):
        """settings.yaml ships "your-gcp-project-id" as a placeholder — that
        must not be treated as a real, configured project."""
        import config as config_module
        conn, db_path = mon_db
        config_module._config = {
            **test_config, "gcp": {"project_id": "your-gcp-project-id"},
        }

        prompt = llm_agent.build_system_prompt("default")

        assert "a MySQL server (managed or self-hosted)" in prompt

    def test_never_raises_when_monitoring_db_unreachable(self, monkeypatch):
        """Defensive: a cold start / misconfigured monitoring DB must fall
        back to safe defaults rather than blocking an analysis."""
        import config as config_module
        config_module._config = {
            "monitoring_db": {"path": "/nonexistent/dir/does/not/exist.db",
                               "wal_mode": False, "busy_timeout_ms": 100},
            "gcp": {},
        }

        prompt = llm_agent.build_system_prompt("default")

        assert "MySQL 8.0+" in prompt
        assert "a MySQL server (managed or self-hosted)" in prompt


class TestRunAnalysisPassesBuiltSystemPrompt:
    """P3-2: run_analysis must thread the per-server built prompt into the
    loop function — not the bare SYSTEM_PROMPT constant."""

    def test_loop_receives_built_system_prompt(self, mon_db, test_config, monkeypatch):
        import config as config_module
        conn, db_path = mon_db
        config_module._config = {
            **test_config, "agent": {"enabled": True, "model": "claude-x"},
        }
        conn.execute(
            "INSERT INTO global_variable_snapshots "
            "(snapshot_time, server_id, variable_name, variable_value) "
            "VALUES ('2026-07-17T00:00:00', 'default', 'version', '8.0.43-google')"
        )
        conn.commit()

        report = MagicMock()
        report.to_markdown.return_value = "## state"
        monkeypatch.setattr(llm_agent, "build_state_report", lambda server_id=None: report)
        monkeypatch.setattr(
            llm_agent, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )

        captured = {}

        def fake_loop(backend, max_tokens, max_rounds, user_msg, system_prompt=None):
            captured["system_prompt"] = system_prompt
            return (
                "### Severity: info\n### Findings\nx\n### Recommendations\ny\n",
                False, 0,
            )

        monkeypatch.setattr(llm_agent, "_run_anthropic_loop", fake_loop)

        # analysis_type="incident" sidesteps the routine skip-quiet path so
        # this test only has to exercise the system-prompt wiring.
        llm_agent.run_analysis("incident", server_id="default")

        assert "system_prompt" in captured
        assert "8.0.43-google" in captured["system_prompt"]
        assert captured["system_prompt"] != llm_agent.SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# P1-27: a brand-new query fingerprint must wake a routine analysis
# ---------------------------------------------------------------------------
class TestIsQuietNewQueries:
    def test_new_queries_present_is_not_quiet(self):
        report = MagicMock()
        report.changes = {"new_queries": [{"digest": "0xABC"}]}
        report.current_state = {}
        assert llm_agent._is_quiet(report) is False

    def test_no_new_queries_and_nothing_else_is_quiet(self):
        report = MagicMock()
        report.changes = {"new_queries": []}
        report.current_state = {}
        assert llm_agent._is_quiet(report) is True


# ---------------------------------------------------------------------------
# P1-22: telemetry columns (model / tool_calls / duration_ms)
# ---------------------------------------------------------------------------
class TestTelemetryColumns:
    def test_run_analysis_stores_telemetry(self, mon_db, test_config, monkeypatch):
        import config as config_module
        conn, db_path = mon_db
        config_module._config = {
            **test_config, "agent": {"enabled": True, "model": "claude-x"},
        }

        report = MagicMock()
        report.to_markdown.return_value = "## state"
        monkeypatch.setattr(llm_agent, "build_state_report", lambda server_id=None: report)
        monkeypatch.setattr(
            llm_agent, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )
        monkeypatch.setattr(
            llm_agent, "_run_anthropic_loop",
            lambda backend, max_tokens, max_rounds, user_msg, system_prompt=None: (
                "### Severity: info\n### Findings\nx\n### Recommendations\ny\n",
                False, 4,
            ),
        )

        result = llm_agent.run_analysis("incident", server_id="default")

        assert result["stored"] is True
        row = conn.execute(
            "SELECT model, tool_calls, duration_ms FROM agent_analyses WHERE id = ?",
            (result["id"],),
        ).fetchone()
        assert row["model"] == "claude-x"
        assert row["tool_calls"] == 4
        assert row["duration_ms"] is not None
        assert row["duration_ms"] >= 0

    def test_run_llm_analysis_stores_telemetry(self, mon_db, test_config, monkeypatch):
        import config as config_module
        conn, db_path = mon_db
        config_module._config = {**test_config, "agent": {}}
        monkeypatch.setattr(
            llm_agent, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )
        monkeypatch.setattr(
            llm_agent, "_run_anthropic_loop",
            lambda backend, max_tokens, max_rounds, prompt, system_prompt=None: (
                "### Severity: info\n### Findings\nx\n### Recommendations\ny\n",
                False, 2,
            ),
        )

        result = llm_agent.run_llm_analysis(
            "some prompt", analysis_type="replay", server_id="default",
        )

        assert result["analysis_id"] is not None
        row = conn.execute(
            "SELECT model, tool_calls, duration_ms FROM agent_analyses WHERE id = ?",
            (result["analysis_id"],),
        ).fetchone()
        assert row["model"] == "claude-x"
        assert row["tool_calls"] == 2
        assert row["duration_ms"] is not None


class TestAgentAnalysesTelemetryMigration:
    """P1-22: the guarded ALTER TABLE migration must be idempotent — safe
    against both a fresh (schema.sql-only) DB and an already-migrated one."""

    def test_columns_present_on_fresh_schema(self, mon_db):
        conn, db_path = mon_db
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_analyses)").fetchall()}
        assert {"model", "tool_calls", "duration_ms"} <= cols

    def test_migration_running_twice_on_already_migrated_db_is_a_noop(
        self, mon_db, test_config
    ):
        import config as config_module
        from storage.migrations import migrate_add_agent_telemetry_columns
        conn, db_path = mon_db
        config_module._config = test_config

        # mon_db's fixture setup already ran the full migration set once
        # (and schema.sql already has the columns), so both direct calls
        # here should find nothing left to add.
        first = migrate_add_agent_telemetry_columns()
        second = migrate_add_agent_telemetry_columns()

        assert first == 0
        assert second == 0

    def test_migration_adds_missing_columns_then_is_idempotent(self, tmp_path):
        """Simulate a DB created before P1-22: agent_analyses exists but
        without the 3 telemetry columns. The migration must add them once,
        then be a no-op on a second run."""
        import sqlite3
        import config as config_module
        from storage.connection import reset_connections
        from storage.migrations import migrate_add_agent_telemetry_columns

        db_path = tmp_path / "pre_migration.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE agent_analyses (
                id INTEGER PRIMARY KEY,
                analyzed_at TEXT NOT NULL,
                server_id TEXT NOT NULL DEFAULT 'default',
                analysis_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                input_summary TEXT,
                findings TEXT,
                recommendations TEXT,
                applied INTEGER NOT NULL DEFAULT 0,
                applied_at TEXT,
                outcome_notes TEXT
            )
        """)
        conn.commit()
        conn.close()

        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
        }
        reset_connections()
        try:
            first = migrate_add_agent_telemetry_columns()
            second = migrate_add_agent_telemetry_columns()
        finally:
            reset_connections()

        assert first == 3   # model, tool_calls, duration_ms all newly added
        assert second == 0  # idempotent: nothing left to add

        conn = sqlite3.connect(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_analyses)").fetchall()}
        conn.close()
        assert {"model", "tool_calls", "duration_ms"} <= cols


# ---------------------------------------------------------------------------
# P1-23: findings/recommendations must not be double-JSON-encoded
# ---------------------------------------------------------------------------
class TestListAnalysesDecodesJsonFields:
    def test_findings_and_recommendations_are_decoded(self, monkeypatch):
        monkeypatch.setattr(agent_routes, "resolve_server_id", lambda server: "default")
        monkeypatch.setattr(
            agent_routes, "query_rows",
            lambda sql, params: [
                {
                    "analyzed_at": "2026-07-17T00:00:00", "analysis_type": "routine",
                    "severity": "warning", "input_summary": "s",
                    "findings": json.dumps("Something bad happened."),
                    "recommendations": json.dumps(["Do X", "Do Y"]),
                    "applied": 0, "outcome_notes": None,
                }
            ],
        )

        rows = agent_routes.list_analyses(limit=20, server="default")

        assert rows[0]["findings"] == "Something bad happened."
        assert rows[0]["recommendations"] == ["Do X", "Do Y"]

    def test_non_json_findings_falls_back_to_raw_string(self, monkeypatch):
        """Defensive: a row that somehow isn't valid JSON (legacy data)
        should pass through unchanged rather than 500."""
        monkeypatch.setattr(agent_routes, "resolve_server_id", lambda server: "default")
        monkeypatch.setattr(
            agent_routes, "query_rows",
            lambda sql, params: [
                {
                    "analyzed_at": "2026-07-17T00:00:00", "analysis_type": "routine",
                    "severity": "warning", "input_summary": "s",
                    "findings": "not valid json {{{",
                    "recommendations": "",
                    "applied": 0, "outcome_notes": None,
                }
            ],
        )

        rows = agent_routes.list_analyses(limit=20, server="default")

        assert rows[0]["findings"] == "not valid json {{{"
        assert rows[0]["recommendations"] == ""
