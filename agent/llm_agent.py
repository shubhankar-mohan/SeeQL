"""
LLM DBA Agent — MySQL analysis powered by Gemini (Vertex AI) or Claude.

Receives a Structured State Report, reasons about it using tool calls,
and produces findings + recommendations stored in agent_analyses.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from agent.state_builder import build_state_report
from agent.tools import TOOL_DEFINITIONS, execute_tool
from agent.prompts import SYSTEM_PROMPT, ROUTINE_ANALYSIS_PROMPT, INCIDENT_ANALYSIS_PROMPT, INCIDENT_TRIGGERS
from config import get_config
from storage import writer
from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)

# Gemini tool definitions (converted from Anthropic format)
GEMINI_TOOL_DEFINITIONS = []
for tool in TOOL_DEFINITIONS:
    props = tool["input_schema"].get("properties", {})
    required = tool["input_schema"].get("required", [])
    gemini_td = {
        "name": tool["name"],
        "description": tool["description"],
    }
    # Gemini requires parameters only if the tool has properties
    if props:
        gemini_td["parameters"] = {
            "type": "object",
            "properties": props,
            "required": required,
        }
    GEMINI_TOOL_DEFINITIONS.append(gemini_td)

# OpenAI / OpenAI-compatible tool definitions (converted from Anthropic format)
OPENAI_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }
    for tool in TOOL_DEFINITIONS
]


def build_system_prompt(server_id: str | None = None) -> str:
    """Format SYSTEM_PROMPT with the real MySQL version + hosting platform
    for `server_id` (P3-2).

    The prompt used to hardcode "a production MySQL 8.0.43 database on GCP
    Cloud SQL" regardless of which server was actually being analyzed —
    wrong context for a self-hosted server, a different major version, or a
    non-GCP deployment.

    mysql_version: the latest `global_variable_snapshots` row for
    variable_name='version', scoped to server_id. Falls back to "8.0+" when
    no snapshot exists yet (e.g. before the first slow-loop cycle) or the
    monitoring DB isn't reachable.
    platform: "a managed Cloud SQL instance" when `gcp.project_id` is
    configured (and isn't the settings.yaml placeholder "your-..."), else
    "a MySQL server (managed or self-hosted)".

    Never raises: any lookup failure here (missing table on an old DB,
    unconfigured monitoring DB, ...) falls back to a safe default so a cold
    start or a misconfigured server never blocks an analysis.
    """
    version = "8.0+"
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                "SELECT variable_value FROM global_variable_snapshots "
                "WHERE variable_name = 'version' AND server_id = ? "
                "ORDER BY snapshot_time DESC LIMIT 1",
                (server_id or "default",),
            ).fetchone()
            if row and row["variable_value"]:
                version = row["variable_value"]
    except Exception as e:
        logger.debug(f"Could not read MySQL version for system prompt: {e}")

    try:
        project_id = (get_config().get("gcp", {}).get("project_id") or "").strip()
    except Exception:
        project_id = ""
    platform = (
        "a managed Cloud SQL instance"
        if project_id and not project_id.startswith("your-")
        else "a MySQL server (managed or self-hosted)"
    )

    return SYSTEM_PROMPT.format(mysql_version=version, platform=platform)


def run_analysis(analysis_type: str = "routine", trigger_type: str | None = None,
                  server_id: str | None = None, incident_id: int | None = None) -> dict | None:
    """
    Run a full LLM analysis cycle.

    Args:
        analysis_type: "routine" (scheduled) or "incident" (triggered by alert)
        trigger_type: For incidents, the alert rule name (e.g. "lock_cascade", "high_cpu").
                      Selects trigger-specific instructions in the incident prompt.
        server_id: Which server to analyze. None = default server.
        incident_id: When set, the stored analysis is linked back to this
                      `incident_windows` row (status -> "analyzed") once stored.
                      Used by the scheduler to close the incident -> analysis loop.

    Returns:
        - Analysis result dict on success (includes `"stored"`/`"truncated"`).
        - None if skipped (agent disabled, no backend configured, or quiet state).
        - `{"status": "error", "error": <str>}` if the LLM loop raised (P1-6) —
          distinguishable from a quiet skip so the API route can 502 honestly.
    """
    config = get_config().get("agent", {})
    if not config.get("enabled", False):
        logger.debug("Agent is disabled in config")
        return None

    # Resolve server_id
    if server_id is None:
        from config.server_registry import get_server_registry
        server_id = get_server_registry().get_default_server_id()

    # Set the current server for live tools
    from agent.tools import set_current_server
    set_current_server(server_id)
    try:
        max_tokens = config.get("max_tokens", 8192)
        max_tool_rounds = config.get("max_tool_rounds", 15)

        # Build the state report for this server
        report = build_state_report(server_id=server_id)
        state_md = report.to_markdown()

        # Skip quiet periods if configured (only for routine, never for incidents)
        if analysis_type == "routine" and config.get("skip_quiet", True) and _is_quiet(report):
            logger.info("State is quiet, skipping analysis")
            return None

        # Select prompt template
        if analysis_type == "incident":
            tt = trigger_type or "default"
            instructions = INCIDENT_TRIGGERS.get(tt, INCIDENT_TRIGGERS["default"])
            user_msg = INCIDENT_ANALYSIS_PROMPT.format(
                trigger_type=tt.replace("_", " ").title(),
                trigger_instructions=instructions,
                state_report=state_md,
            )
        else:
            user_msg = ROUTINE_ANALYSIS_PROMPT.format(state_report=state_md)

        # Determine backend: Gemini (Vertex AI) or Claude (Anthropic)
        backend = _detect_backend(config)
        if backend is None:
            return None

        # Per-server system prompt (P3-2): real MySQL version + platform for
        # this server_id, instead of the hardcoded constant.
        system_prompt = build_system_prompt(server_id)

        logger.info(f"Running {analysis_type} analysis with {backend['type']} ({backend['model']})")

        start_time = time.time()
        try:
            if backend["type"] == "gemini":
                result, truncated, tool_calls = _run_gemini_loop(
                    backend, max_tokens, max_tool_rounds, user_msg, system_prompt
                )
            elif backend["type"] == "vertex-claude":
                result, truncated, tool_calls = _run_vertex_claude_loop(
                    backend, max_tokens, max_tool_rounds, user_msg, system_prompt
                )
            elif backend["type"] == "openai":
                result, truncated, tool_calls = _run_openai_loop(
                    backend, max_tokens, max_tool_rounds, user_msg, system_prompt
                )
            else:
                result, truncated, tool_calls = _run_anthropic_loop(
                    backend, max_tokens, max_tool_rounds, user_msg, system_prompt
                )
        except Exception as e:
            # Honest failure (P1-6): a loop exception (e.g. a non-retryable LLM
            # API error, or a retryable one that never recovered) used to be
            # swallowed into the same `None` a caller also gets for "agent
            # disabled" / "quiet state" — the API route then reported a real
            # failure as "skipped". Return a distinguishable error shape so the
            # route can respond honestly (502) instead.
            logger.error(f"Agent analysis failed: {e}")
            return {"status": "error", "error": str(e)}
        duration_ms = int((time.time() - start_time) * 1000)
    finally:
        # Reset the target-server ContextVar so a pooled worker thread can't
        # leak it into a later call (P1-10). This now covers the WHOLE
        # region from state-report building through backend detection and
        # the LLM loop -- previously the finally only wrapped the loop
        # itself, so an exception raised while building the state report
        # (e.g. a bad snapshot query) skipped the reset entirely and left
        # this worker thread's ContextVar pointed at `server_id` for
        # whatever job APScheduler runs next on it.
        set_current_server(None)

    # Parse and store the result
    analysis = _parse_and_store(
        result, analysis_type, state_md, server_id, incident_id=incident_id,
        truncated=truncated, model=backend["model"], tool_calls=tool_calls,
        duration_ms=duration_ms,
    )
    return analysis


def _looks_like_openai(model: str) -> bool:
    """Heuristic: an OpenAI-family model name (gpt-*, o1/o3/o4-*, openai/*)."""
    m = (model or "").lower()
    return (
        m.startswith(("gpt-", "gpt", "o1", "o3", "o4", "openai/", "chatgpt"))
    )


def _cfg_value(config: dict, key: str) -> str | None:
    """Read a config secret, treating an unsubstituted ${VAR} placeholder as unset."""
    v = config.get(key)
    if isinstance(v, str) and v.startswith("${"):
        return None
    return v or None


def _adc_available() -> bool:
    """True when google-auth can resolve any default credentials (env var, ADC file, metadata)."""
    try:
        import google.auth
        google.auth.default()
        return True
    except Exception:
        return False


def _detect_backend(config: dict) -> dict | None:
    """Detect which LLM backend to use.

    An explicit `agent.provider` takes precedence; otherwise selection is
    model-name-driven, then falls back to whatever credentials are available.

    Supported providers:
      - "vertex-claude" : Claude via Vertex AI       (claude-* model + GCP creds)
      - "anthropic"     : Claude via the Anthropic API (claude-* model + API key)
      - "gemini"        : Gemini via Vertex AI         (gemini-* model + GCP creds)
      - "openai"        : OpenAI **or any OpenAI-compatible endpoint** — set
                          `openai_base_url` to point at Azure OpenAI, Ollama,
                          vLLM, Groq, OpenRouter, LM Studio, etc. This is the
                          "bring your own / any other LLM" path.
    """
    gcp_config = get_config().get("gcp", {})
    project_id = gcp_config.get("project_id")
    if project_id in (None, "", "your-gcp-project-id"):
        project_id = None
    model = config.get("model", "gemini-2.5-flash")
    has_gcp_creds = bool(project_id) and (bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")) or _adc_available())

    anthropic_key = _cfg_value(config, "anthropic_api_key")
    openai_key = _cfg_value(config, "openai_api_key") or os.environ.get("OPENAI_API_KEY")
    openai_base_url = _cfg_value(config, "openai_base_url") or os.environ.get("OPENAI_BASE_URL")

    def _vertex_claude():
        return {"type": "vertex-claude", "model": model, "project_id": project_id,
                "region": gcp_config.get("claude_region") or gcp_config.get("vertex_region", "us-east5")}

    def _anthropic():
        return {"type": "anthropic", "model": model, "api_key": anthropic_key}

    def _gemini(m):
        return {"type": "gemini", "model": m, "project_id": project_id,
                "region": gcp_config.get("gemini_region") or gcp_config.get("vertex_region", "us-central1")}

    def _openai():
        return {"type": "openai", "model": model, "api_key": openai_key,
                "base_url": openai_base_url}

    # --- Explicit provider override (required for OpenAI-compatible servers
    #     whose model names don't match any prefix) -----------------------------
    provider = (config.get("provider") or "").strip().lower().replace("_", "-") or None
    if provider:
        if provider in ("openai", "openai-compatible", "custom"):
            if openai_key or openai_base_url:
                return _openai()
            logger.warning("provider=openai but no openai_api_key / openai_base_url configured")
            return None
        if provider == "anthropic":
            return _anthropic() if anthropic_key else _missing("ANTHROPIC_API_KEY")
        if provider == "vertex-claude":
            return _vertex_claude() if has_gcp_creds else _missing("GCP credentials")
        if provider == "gemini":
            return _gemini(model if model.startswith("gemini") else "gemini-2.5-flash") \
                if has_gcp_creds else _missing("GCP credentials")
        logger.warning("Unknown agent.provider %r; falling back to auto-detection.", provider)

    # --- Model-name-driven detection ------------------------------------------
    if _looks_like_openai(model) and (openai_key or openai_base_url):
        return _openai()
    if model.startswith("claude"):
        if anthropic_key and has_gcp_creds:
            # Both are configured (P1-28): prefer the Anthropic API — an
            # explicit key is a deliberate choice, whereas GCP credentials
            # may just be ambiently present (e.g. for the gemini fallback
            # path or other GCP services) rather than intended for Claude.
            logger.info(
                "claude-* model has both GCP credentials and ANTHROPIC_API_KEY "
                "configured; preferring the Anthropic API (explicit key) over Vertex AI."
            )
            return _anthropic()
        if has_gcp_creds:
            return _vertex_claude()
        if anthropic_key:
            return _anthropic()
    if model.startswith("gemini") and has_gcp_creds:
        return _gemini(model)

    # --- Fallback by whatever credentials exist -------------------------------
    if has_gcp_creds:
        if not model.startswith("gemini"):
            logger.warning("Model %r has no matching backend/creds; using gemini-2.5-flash "
                           "via Vertex AI (GCP creds present).", model)
            model = "gemini-2.5-flash"
        return _gemini(model)
    if anthropic_key:
        if not model.startswith("claude"):
            fallback = config.get("fallback_model", "claude-sonnet-4-20250514")
            logger.warning("Model %r has no matching backend/creds; using "
                           "%s via the Anthropic API.", model, fallback)
            model = fallback
        return _anthropic()
    if openai_key or openai_base_url:
        return _openai()

    logger.warning("No LLM credentials configured (need GOOGLE_APPLICATION_CREDENTIALS, "
                   "ANTHROPIC_API_KEY, or OPENAI_API_KEY / openai_base_url).")
    return None


def _missing(what: str) -> None:
    logger.warning("Configured agent.provider requires %s, which is not available.", what)
    return None


# repr(exception) substrings (matched case-insensitively) that indicate a
# transient provider error worth retrying: rate limits (429), overload
# (529 / "overloaded"), general rate-limit wording, upstream unavailability,
# and request timeouts. Anything else (auth, bad request, schema errors) is
# not retried.
# "rate" alone used to be a marker here, but it false-matched non-retryable
# errors mentioning "temperature", "generate", "moderate", "separate", etc.,
# wasting a ~10s backoff sleep on an error that was never going to succeed on
# retry (PR-4 review) — narrowed to the actual rate-limit phrasings.
_RETRYABLE_ERROR_MARKERS = (
    "429", "529", "overloaded", "rate limit", "rate_limit", "unavailable", "timeout",
)

# Backoff between attempts: 2s after the 1st failure, 8s after the 2nd, ...
# (the last configured delay repeats if `attempts` is raised beyond this).
_RETRY_BACKOFF_SECONDS = (2, 8)


def _create_with_retry(fn, *, attempts: int = 3):
    """Call `fn()`, retrying on transient LLM provider errors (P1-6).

    A single 429/529/overload used to discard every tool round already
    completed in the loop, and `run_analysis` had no way to tell that
    failure apart from "agent disabled" / "quiet state" (both return None).
    This wraps every provider `create()` call so a transient blip doesn't
    throw away completed work.

    Retries (up to `attempts` total calls) when `repr(exception)` contains
    one of `_RETRYABLE_ERROR_MARKERS`, sleeping `_RETRY_BACKOFF_SECONDS`
    between attempts. A non-retryable error re-raises immediately without
    sleeping; a retryable error still re-raises once `attempts` is
    exhausted, so the caller can report an honest failure.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            retryable = any(marker in repr(e).lower() for marker in _RETRYABLE_ERROR_MARKERS)
            if not retryable or attempt == attempts - 1:
                raise
            delay = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "Retryable LLM provider error (attempt %d/%d): %r — retrying in %ss",
                attempt + 1, attempts, e, delay,
            )
            time.sleep(delay)


# Appended as a final user turn when a loop exhausts max_rounds while the
# model is still calling tools (P1-5), with tool-calling disabled for that
# one call so the model is forced to summarize instead of asking for more.
_FINALIZE_NUDGE = (
    "Tool budget exhausted. Produce your final structured analysis now from "
    "the evidence gathered. Do not call tools."
)


def _run_gemini_loop(backend: dict, max_tokens: int, max_rounds: int,
                      user_msg: str, system_prompt: str = SYSTEM_PROMPT) -> tuple[str, bool, int]:
    """Run tool-use loop with Gemini via Vertex AI.

    Returns (final_text, truncated, total_tool_calls) — `truncated` is True
    if ANY response during the loop (a tool-calling round or the max-rounds
    finalize call) was cut off at MAX_TOKENS (P1-13); the flag is monotonic,
    so a mid-stream truncation stays flagged even if a later round completes
    cleanly. `total_tool_calls` is the count of tool calls executed across
    the whole loop (P1-22 telemetry).

    `system_prompt` defaults to the bare SYSTEM_PROMPT constant so direct
    callers/tests don't have to supply one; `run_analysis`/`run_llm_analysis`
    pass the per-server prompt built by `build_system_prompt` (P3-2).
    """
    from google import genai
    from google.genai import types

    client = genai.Client(
        vertexai=True,
        project=backend["project_id"],
        location=backend["region"],
    )

    # Build tools
    tools = types.Tool(function_declarations=[
        types.FunctionDeclaration(**td) for td in GEMINI_TOOL_DEFINITIONS
    ])

    # System instruction + initial message
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_msg)])]

    def _response_text(response) -> str:
        """Concatenated text parts, tolerating empty/None candidates or parts
        (safety blocks, MAX_TOKENS with no emitted content)."""
        candidates = response.candidates or []
        if not candidates:
            return ""
        candidate_content = candidates[0].content
        parts = (candidate_content.parts if candidate_content else None) or []
        return "\n".join(part.text for part in parts if part.text)

    def _response_truncated(response) -> bool:
        candidates = response.candidates or []
        if not candidates:
            return False
        finish_reason = getattr(candidates[0], "finish_reason", None)
        return "MAX_TOKENS" in str(finish_reason or "").upper()

    final_text = ""
    truncated = False
    total_tool_calls = 0
    for round_num in range(max_rounds):
        logger.info(f"  Agent round {round_num + 1}/{max_rounds}")
        response = _create_with_retry(lambda: client.models.generate_content(
            model=backend["model"],
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[tools],
                max_output_tokens=max_tokens,
                temperature=0,
            ),
        ))

        if _response_truncated(response):
            logger.warning("  Gemini response truncated (finish_reason=MAX_TOKENS)")
            truncated = True

        # Gemini can return no candidates (safety/recitation block, or
        # MAX_TOKENS with no emitted content), and a candidate's `parts` can be
        # None. Guard both so a common response shape doesn't raise IndexError
        # and silently kill the analysis.
        candidates = response.candidates or []
        if not candidates:
            logger.warning(
                "Gemini returned no candidates (safety/recitation block or empty "
                "response); ending tool loop"
            )
            break
        candidate_content = candidates[0].content
        parts = (candidate_content.parts if candidate_content else None) or []

        # Collect text and function calls from response
        text_parts = []
        function_calls = []
        for part in parts:
            if part.text:
                text_parts.append(part.text)
            if part.function_call:
                function_calls.append(part.function_call)

        if text_parts:
            final_text = "\n".join(text_parts)

        # If no function calls, we're done
        if not function_calls:
            logger.info(f"  Agent completed after {round_num + 1} rounds, {total_tool_calls} tool calls")
            break

        # Add assistant response to contents
        contents.append(candidate_content)

        # Execute tools and add results
        tool_response_parts = []
        for fc in function_calls:
            name = fc.name
            args = dict(fc.args) if fc.args else {}
            logger.info(f"  Tool call [{total_tool_calls + 1}]: {name}({json.dumps(args)[:200]})")
            result = execute_tool(name, args)
            total_tool_calls += 1
            # execute_tool() returns a JSON string (see agent/tools.py); hand
            # Gemini the PARSED object instead of a JSON-string-within-a-
            # string, which forced the model to re-parse a serialized blob
            # on every tool result (P1-24). Falls back to the raw string for
            # the rare handler that doesn't return valid JSON.
            try:
                parsed_result = json.loads(result)
            except (TypeError, json.JSONDecodeError):
                parsed_result = result
            # Key parallel same-turn tool responses by id, not just name, so
            # two calls to the same tool in one turn can't get crossed
            # results (P1-12). `from_function_response()` in the installed
            # google-genai SDK has no `id` kwarg, so build the FunctionResponse
            # directly; `id=None` (no id on the call) serializes the same as
            # the classmethod, so this one path covers both cases.
            tool_response_parts.append(types.Part(function_response=types.FunctionResponse(
                id=getattr(fc, "id", None), name=name, response={"result": parsed_result},
            )))

        contents.append(types.Content(role="user", parts=tool_response_parts))
    else:
        # Max rounds reached and the model was still calling tools (P1-5):
        # make one final call with tools disabled so the model summarizes
        # whatever evidence it already gathered, instead of storing "" or
        # stale text left over from an earlier round.
        logger.warning(f"  Agent hit max rounds ({max_rounds}) with {total_tool_calls} tool calls")
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=_FINALIZE_NUDGE)])
        )
        finalize_response = _create_with_retry(lambda: client.models.generate_content(
            model=backend["model"],
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
                temperature=0,
            ),
        ))
        if _response_truncated(finalize_response):
            logger.warning("  Gemini finalize response truncated (finish_reason=MAX_TOKENS)")
            truncated = True
        final_text = _response_text(finalize_response)

    return final_text, truncated, total_tool_calls


def _run_vertex_claude_loop(backend: dict, max_tokens: int, max_rounds: int, user_msg: str,
                             system_prompt: str = SYSTEM_PROMPT) -> tuple[str, bool, int]:
    """Run tool-use loop with Claude via Vertex AI (GCP credentials)."""
    try:
        from anthropic import AnthropicVertex
    except ImportError as e:
        # AnthropicVertex imports google-auth at module load, and google-auth
        # ships only with the [gcp] extra — not the core install. Surface an
        # actionable message instead of a cryptic "No module named 'google.auth'".
        raise RuntimeError(
            "Claude via Vertex AI needs the GCP dependencies. Install them with "
            "`pip install 'seeql[gcp]'`, or set ANTHROPIC_API_KEY to use the "
            "Anthropic API instead."
        ) from e
    client = AnthropicVertex(
        project_id=backend["project_id"],
        region=backend["region"],
    )
    return _run_claude_loop(client, backend["model"], max_tokens, max_rounds, user_msg, system_prompt)


def _run_anthropic_loop(backend: dict, max_tokens: int, max_rounds: int, user_msg: str,
                         system_prompt: str = SYSTEM_PROMPT) -> tuple[str, bool, int]:
    """Run tool-use loop with Claude via Anthropic API."""
    import anthropic
    client = anthropic.Anthropic(api_key=backend["api_key"])
    return _run_claude_loop(client, backend["model"], max_tokens, max_rounds, user_msg, system_prompt)


def _openai_token_kwargs(model: str, max_tokens: int) -> dict:
    """Token/sampling kwargs for chat.completions.create() (P1-11).

    o-series reasoning models (o1/o3/o4/...) reject `max_tokens` and
    `temperature` outright (400) — they take `max_completion_tokens` instead
    and have no sampling temperature to set.
    """
    if re.match(r"^o\d", model):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens, "temperature": 0}


def _run_openai_loop(backend: dict, max_tokens: int, max_rounds: int, user_msg: str,
                      system_prompt: str = SYSTEM_PROMPT) -> tuple[str, bool, int]:
    """Tool-use loop for OpenAI and any OpenAI-compatible endpoint.

    A `base_url` in the backend points the OpenAI SDK at a compatible server
    (Azure OpenAI, Ollama, vLLM, Groq, OpenRouter, LM Studio, …), so this single
    loop covers "OpenAI" and "bring your own / any other LLM".

    Returns (final_text, truncated, total_tool_calls) — see _run_gemini_loop
    for the shared contract (P1-13 truncated, P1-22 total_tool_calls).
    `system_prompt` defaults to the bare SYSTEM_PROMPT constant; the real
    callers pass the per-server prompt from `build_system_prompt` (P3-2).
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "The OpenAI backend needs the 'openai' package. Install it with "
            "`pip install 'seeql[openai]'`."
        ) from e

    api_key = backend.get("api_key")
    base_url = backend.get("base_url")
    # OpenAI-compatible servers (Ollama, vLLM, …) usually ignore the key but the
    # SDK still requires a non-empty value, so supply a placeholder for base_url.
    if base_url and not api_key:
        api_key = "not-needed"
    client_kwargs = {}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    model = backend["model"]
    # Invariant for the whole call — derive once (o-series vs. standard token
    # params, P1-11) rather than per round + on the finalize call.
    token_kwargs = _openai_token_kwargs(model, max_tokens)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    final_text = ""
    truncated = False
    total_tool_calls = 0

    for round_num in range(max_rounds):
        logger.info(f"  Agent round {round_num + 1}/{max_rounds}")
        response = _create_with_retry(lambda: client.chat.completions.create(
            model=model,
            tools=OPENAI_TOOL_DEFINITIONS,
            messages=messages,
            **token_kwargs,
        ))
        choices = response.choices or []
        if not choices:
            logger.warning("OpenAI-compatible endpoint returned no choices; ending tool loop")
            break
        if getattr(choices[0], "finish_reason", None) == "length":
            logger.warning("  OpenAI response truncated (finish_reason=length)")
            truncated = True
        msg = choices[0].message
        if msg.content:
            final_text = msg.content

        tool_calls = msg.tool_calls or []
        if not tool_calls:
            logger.info(f"  Agent completed after {round_num + 1} rounds, {total_tool_calls} tool calls")
            break

        # Echo the assistant turn (must include the tool_calls), then the results.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            logger.info(f"  Tool call [{total_tool_calls + 1}]: {tc.function.name}({json.dumps(args)[:200]})")
            result = execute_tool(tc.function.name, args)
            total_tool_calls += 1
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result if isinstance(result, str) else json.dumps(result),
            })
    else:
        # Max rounds reached and the model was still calling tools (P1-5):
        # make one final call with tools omitted so the model summarizes
        # whatever evidence it already gathered, instead of storing "" or
        # stale text left over from an earlier round.
        logger.warning(f"  Agent hit max rounds ({max_rounds}) with {total_tool_calls} tool calls")
        messages.append({"role": "user", "content": _FINALIZE_NUDGE})
        finalize_response = _create_with_retry(lambda: client.chat.completions.create(
            model=model,
            messages=messages,
            **token_kwargs,
        ))
        choices = finalize_response.choices or []
        if choices:
            if getattr(choices[0], "finish_reason", None) == "length":
                logger.warning("  OpenAI finalize response truncated (finish_reason=length)")
                truncated = True
            final_text = choices[0].message.content or ""
        else:
            final_text = ""

    return final_text, truncated, total_tool_calls


def _run_claude_loop(client, model: str, max_tokens: int, max_rounds: int, user_msg: str,
                      system_prompt: str = SYSTEM_PROMPT) -> tuple[str, bool, int]:
    """Shared tool-use loop for any Claude client (API or Vertex).

    Returns (final_text, truncated, total_tool_calls) — `truncated` is True
    if ANY response during the loop (a tool-calling round or the max-rounds
    finalize call) ended on stop_reason "max_tokens" (P1-13); the flag is
    monotonic, so a mid-stream truncation stays flagged even if a later
    round completes cleanly. `total_tool_calls` is the count of tool calls
    executed across the whole loop (P1-22 telemetry). `system_prompt`
    defaults to the bare SYSTEM_PROMPT constant; the real callers pass the
    per-server prompt from `build_system_prompt` (P3-2).
    """
    messages = [{"role": "user", "content": user_msg}]
    final_text = ""
    truncated = False
    total_tool_calls = 0

    for round_num in range(max_rounds):
        logger.info(f"  Agent round {round_num + 1}/{max_rounds}")
        response = _create_with_retry(lambda: client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages,
            temperature=0,
        ))

        if response.stop_reason == "max_tokens":
            logger.warning("  Claude response truncated (stop_reason=max_tokens)")
            truncated = True

        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if text_parts:
            final_text = "\n".join(text_parts)

        if not tool_uses or response.stop_reason == "end_turn":
            logger.info(f"  Agent completed after {round_num + 1} rounds, {total_tool_calls} tool calls")
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_use in tool_uses:
            logger.info(f"  Tool call [{total_tool_calls + 1}]: {tool_use.name}({json.dumps(tool_use.input)[:200]})")
            result = execute_tool(tool_use.name, tool_use.input)
            total_tool_calls += 1
            tool_result = {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
            }
            # Flag tool-level errors so Claude treats them as failed calls
            # rather than legitimate data (P1-26). execute_tool() always
            # serializes errors as `{"error": ...}` (see agent/tools.py).
            if isinstance(result, str) and result.startswith('{"error'):
                tool_result["is_error"] = True
            tool_results.append(tool_result)

        messages.append({"role": "user", "content": tool_results})
    else:
        # Max rounds reached and the model was still calling tools (P1-5):
        # make one final call with tool use disabled so the model summarizes
        # whatever evidence it already gathered, instead of storing "" or
        # stale text left over from an earlier round.
        logger.warning(f"  Agent hit max rounds ({max_rounds}) with {total_tool_calls} tool calls")
        messages.append({"role": "user", "content": _FINALIZE_NUDGE})
        finalize_response = _create_with_retry(lambda: client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            tool_choice={"type": "none"},
            messages=messages,
            temperature=0,
        ))
        if finalize_response.stop_reason == "max_tokens":
            logger.warning("  Claude finalize response truncated (stop_reason=max_tokens)")
            truncated = True
        final_text = "\n".join(
            block.text for block in finalize_response.content if block.type == "text"
        )

    return final_text, truncated, total_tool_calls


def _is_quiet(report) -> bool:
    """Check if the state report has nothing interesting to analyze."""
    changes = report.changes
    cs = report.current_state

    # Not quiet if there are regressions, DDL changes, deadlocks, or locks
    if changes.get("regressions"):
        return False
    if changes.get("new_queries"):
        # A brand-new (never-before-seen) heavy query fingerprint deserves a
        # look even if nothing else in the state report looks alarming yet —
        # skipping it here meant a new query could run unreviewed for a full
        # skip-quiet cycle before anything noticed it (P1-27).
        return False
    if changes.get("ddl_changes"):
        return False
    if changes.get("deadlocks"):
        return False
    if cs.get("lock_waits", {}).get("lock_count", 0) > 0:
        return False
    if cs.get("long_transactions"):
        return False
    if cs.get("anomalies"):
        return False

    return True


def _resolve_addressed_incident(
    incident_id: int | None, self_reported: int | None,
    analysis_type: str, source_text: str,
) -> int | None:
    """Decide which incident (if any) an analysis should link to (P1-4).

    An explicit `incident_id` — the caller (scheduler/replay) driving this
    analysis *at* a specific incident — is authoritative and is never gated.

    A model's self-reported `### Addresses incident #N` is a different
    thing: a blind "routine" run has no incident in its prompt at all, so an
    unprompted self-report there is far more likely to be hallucinated than
    real — it's honored only when the id genuinely appears in the text the
    model was shown. Non-routine runs (incident/investigation/replay) are
    already triggered with real incident context, so their self-report is
    trusted outright.
    """
    if incident_id is not None:
        return incident_id
    if self_reported is None:
        return None
    if analysis_type != "routine" or re.search(rf"#{self_reported}\b", source_text or ""):
        return self_reported
    logger.warning(
        f"Ignoring self-reported '### Addresses incident #{self_reported}' from a "
        f"{analysis_type} analysis — id not present in the text the model was shown"
    )
    return None


def _build_outcome_notes(confidence: float | None, truncated: bool) -> str | None:
    """Serialize the extra per-analysis signals stashed in `outcome_notes`.

    There are no dedicated `confidence`/`truncated` columns on
    agent_analyses, so both are stored as JSON in `outcome_notes` rather than
    a schema migration. Shared by `_parse_and_store` and `run_llm_analysis`
    (P1-18: those two store paths are near-twins that have drifted before)
    so a future signal added here can't silently land in only one of them.
    Returns None when there's nothing to record (keeps the column NULL).
    """
    outcome = {}
    if confidence is not None:
        outcome["confidence"] = confidence
    if truncated:
        outcome["truncated"] = True
    return json.dumps(outcome) if outcome else None


def _parse_and_store(text: str, analysis_type: str, input_summary: str,
                     server_id: str = "default", incident_id: int | None = None,
                     truncated: bool = False, model: str | None = None,
                     tool_calls: int | None = None,
                     duration_ms: int | None = None) -> dict:
    """Parse agent response and store in agent_analyses table.

    If `incident_id` is given (or the response self-reports one via
    `### Addresses incident #N` and that self-report passes
    `_resolve_addressed_incident`'s gate — P1-4), the newly-stored analysis
    is linked to that `incident_windows` row in the same transaction as the
    insert via `storage.writer.write_agent_analysis_and_link` (P1-21) —
    best-effort, never raises. The link itself never resurrects a
    resolved/closed incident and never crosses server_id.

    If the response has no usable text after parsing (max-rounds exhaustion
    that never produced a finalize answer, or a Gemini safety block — P1-5),
    nothing is written and no incident is linked: the caller gets back
    `{"stored": False, ...}` and any incident is left `detected` so it can
    be retried, instead of being silently marked "analyzed" forever with an
    empty analysis attached.
    """
    # One shared parser for severity/sections/confidence/addresses (P1-18) —
    # see `parse_agent_response` below for the tolerant-form fixes (P1-2,
    # P1-3, P1-19, P3-5, P3-9).
    parsed = parse_agent_response(text)
    if not parsed["cleaned"]:
        logger.error(
            f"Agent response for {analysis_type} analysis was empty after parsing "
            "(max-rounds exhaustion or safety block) — not storing, not linking any incident"
        )
        return {
            "stored": False,
            "id": None,
            "severity": None,
            "findings": None,
            "recommendations": None,
            "confidence": None,
            "truncated": truncated,
            "raw_response": text,
        }

    severity = parsed["severity"]
    findings = parsed["findings"]
    recommendations = parsed["recommendations"]
    confidence = parsed["confidence"]
    addressed_incident = _resolve_addressed_incident(
        incident_id, parsed["addresses_incident"], analysis_type, input_summary
    )

    analysis = {
        "analyzed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "server_id": server_id,
        "analysis_type": analysis_type,
        "severity": severity,
        "input_summary": input_summary[:2000],
        "findings": json.dumps(findings),
        "recommendations": json.dumps(recommendations),
        "applied": 0,
        "outcome_notes": _build_outcome_notes(confidence, truncated),
        "model": model,
        "tool_calls": tool_calls,
        "duration_ms": duration_ms,
    }

    analysis_id = None
    try:
        analysis_id, linked = writer.write_agent_analysis_and_link(
            analysis, addressed_incident, server_id
        )
        logger.info(f"Stored {analysis_type} analysis (severity={severity})")
        if addressed_incident is not None and not linked:
            logger.info(
                f"Analysis {analysis_id} not linked to incident {addressed_incident} "
                "(incident is not open on this server)"
            )
    except Exception as e:
        logger.error(f"Failed to store/link analysis: {e}")

    analysis["id"] = analysis_id
    analysis["raw_response"] = text
    analysis["stored"] = analysis_id is not None
    analysis["truncated"] = truncated

    return analysis


# Parser contract for the confidence + incident-linkage headers (P1.3 / E(a)):
#   ### Confidence: 0.82 — strong evidence
#   ### Addresses incident #7
#
# Tolerant forms (parser correctness batch, Task 4.2 — P1-2 / P1-19): the
# severity regex used to be `^#{2,3}\s*Severity:` — stricter than everything
# else in this file — so `**Severity:** critical` (bold), `#### Severity:`
# (4+ hashes), and looser punctuation between label and value all silently
# defaulted to "info", defeating every downstream severity filter.
# Confidence used to reject a leading-dot decimal (".85") and never clamped
# values above 1. Addresses used to reject a colon before the "#"
# ("incident: #7").
_SEV_RE = re.compile(
    r'^\s*(?:#{1,6}\s*|\*\*)\s*Severity\s*[:*\s-]+\s*(critical|warning|info)',
    re.IGNORECASE | re.MULTILINE,
)
_CONF_RE = re.compile(
    r'^\s*(?:#{1,6}\s*|\*\*|-\s*\*\*)\s*Confidence\s*[:*]*\s*[:\s]\s*(\d?\.\d+|[01])',
    re.IGNORECASE | re.MULTILINE,
)
_ADDR_RE = re.compile(
    r'^\s*(?:#{1,6}\s*|\*\*)\s*Addresses\s+incident\s*[:#]*\s*#?(\d+)',
    re.IGNORECASE | re.MULTILINE,
)


def _extract_confidence(text: str) -> float | None:
    """Parse the `### Confidence: 0.xx` header line, if present (tolerant
    forms + clamped to [0, 1] — P1-19)."""
    m = _CONF_RE.search(text or "")
    return min(1.0, float(m.group(1))) if m else None


def _extract_addresses_incident(text: str) -> int | None:
    """Parse the `### Addresses incident #N` header line, if present
    (tolerant forms — P1-19)."""
    m = _ADDR_RE.search(text or "")
    return int(m.group(1)) if m else None


def _strip_outer_fence(text: str) -> str:
    """Strip a single OUTER code fence wrapping the entire payload — Gemini
    sometimes wraps its whole response in a ```markdown block.

    A single DOTALL match anchored to the start/end of the (stripped) text —
    never a per-line MULTILINE substitution — so an inner ```sql fence
    nested inside a Recommendations section survives intact (P1-3).
    """
    m = re.match(r'^```(?:markdown)?\s*\n(.*)\n```\s*$', text.strip(), re.DOTALL)
    return m.group(1) if m else text


def parse_agent_response(text: str) -> dict:
    """Parse a raw LLM analysis response into its structured parts.

    The single shared parser used by both `_parse_and_store` (routine /
    incident analyses) and `run_llm_analysis` (replay / investigator) —
    P1-18: these used to be two independently-maintained copies of the same
    severity/section/confidence/addresses regex logic that had already
    drifted from each other.
    """
    cleaned = _strip_outer_fence(text or "").strip()
    first_header = cleaned.find("###")
    if first_header > 0:
        cleaned = cleaned[first_header:]          # drop model preamble (P3-9)
    sev = _SEV_RE.search(cleaned)
    severity = sev.group(1).lower() if sev else "info"
    if not sev and cleaned:
        logger.warning("Agent response has no Severity header; defaulting to info")
    findings, recommendations = _split_findings_recommendations(cleaned)
    conf = _CONF_RE.search(cleaned)
    confidence = min(1.0, float(conf.group(1))) if conf else None
    addr = _ADDR_RE.search(cleaned)
    return {"cleaned": cleaned, "severity": severity, "findings": findings,
            "recommendations": recommendations, "confidence": confidence,
            "addresses_incident": int(addr.group(1)) if addr else None}


# Regex to find markdown section headers like "### Findings", "## FINDINGS", "**Findings**"
_SECTION_RE_CACHE = {}

def _section_pattern(name: str) -> re.Pattern:
    if name not in _SECTION_RE_CACHE:
        # Note: use string concatenation, not f-string — f-string mangles {2,3} quantifiers.
        # No trailing `$` (P3-5): tolerates inline text after the header on the same
        # line, e.g. "### Findings: No change since analysis #4", while the trailing
        # `\s*` still eats the newline before real body text on the standard
        # multi-line form ("### Findings\n<body>").
        _SECTION_RE_CACHE[name] = re.compile(
            r'^(?:#{2,3}\s*|\*\*)' + name + r'(?:\*\*)?\s*[:]?\s*',
            re.IGNORECASE | re.MULTILINE,
        )
    return _SECTION_RE_CACHE[name]


def _extract_section(text: str, start_name: str, end_name: str | None) -> str:
    """Extract text between two markdown section headers (case-insensitive)."""
    start_match = _section_pattern(start_name).search(text)
    if not start_match:
        return ""

    start_idx = start_match.end()

    if end_name:
        end_match = _section_pattern(end_name).search(text, start_idx)
        if end_match:
            return text[start_idx:end_match.start()].strip()

    return text[start_idx:].strip()


def _split_findings_recommendations(cleaned: str) -> tuple[str, str]:
    """Split an analysis blob into (findings, recommendations).

    Handles both the standard agent format (`### Findings` / `### Recommendations`)
    and the richer replay/investigator format (singular `### Recommendation`, with
    no explicit Findings header) so neither DB column is left empty.
    """
    findings = _extract_section(cleaned, "findings", "recommendations")
    recommendations = _extract_section(cleaned, "recommendations", None)
    if not recommendations:
        recommendations = _extract_section(cleaned, "recommendation", None)

    if not findings and not recommendations:
        findings = cleaned
    elif not findings:
        # A recommendations section parsed but there's no explicit "### Findings"
        # header (replay/RCA format). Treat everything before the recommendation
        # as findings.
        for header in ("recommendations", "recommendation"):
            m = _section_pattern(header).search(cleaned)
            if m:
                findings = cleaned[:m.start()].strip()
                break
    return findings, recommendations


# ---------------------------------------------------------------------------
# Public wrapper for arbitrary LLM analyses (Phase 1.7)
# ---------------------------------------------------------------------------

def run_llm_analysis(
    prompt: str,
    analysis_type: str = "replay",
    server_id: str | None = None,
    tool_budget=None,
    max_tool_rounds_override: int | None = None,
    incident_id: int | None = None,
) -> dict:
    """
    Public wrapper that dispatches any custom prompt to the configured LLM
    backend, stores the result in `agent_analyses`, and returns
    `{"text": str, "analysis_id": int, "severity": str, "confidence": float,
    "truncated": bool}` (P1-13 — True if the final response was cut off at
    max_tokens).

    Used by `agent.replay.run_replay` so the replay module doesn't have to
    reimplement backend detection or row-id wiring. Raises RuntimeError if
    no LLM backend is configured — callers should catch and fall back to
    the timeline-only rendering.

    The webhook investigator passes `tool_budget` (an `alerting.budget.Budget`)
    and a lower `max_tool_rounds_override` so Phase 2 stays bounded.

    incident_id: When set (or when the response self-reports one via
    `### Addresses incident #N` and that self-report passes
    `_resolve_addressed_incident`'s gate — P1-4), the stored analysis is
    linked to that `incident_windows` row in the same transaction as the
    insert via `storage.writer.write_agent_analysis_and_link` (P1-21) —
    best-effort, never raises. The link never resurrects a resolved/closed
    incident and never crosses server_id.
    """
    config = get_config().get("agent", {})
    backend = _detect_backend(config)
    if backend is None:
        raise RuntimeError("No LLM backend configured")

    if server_id is None:
        from config.server_registry import get_server_registry
        server_id = get_server_registry().get_default_server_id()

    # Per-server system prompt (P3-2): real MySQL version + platform for
    # this server_id, instead of the hardcoded constant.
    system_prompt = build_system_prompt(server_id)

    from agent.tools import set_current_server, set_current_budget
    try:
        set_current_server(server_id)
    except Exception:
        pass
    set_current_budget(tool_budget)

    max_tokens = config.get("max_tokens", 8192)
    max_tool_rounds = (
        max_tool_rounds_override
        if max_tool_rounds_override is not None
        else config.get("max_tool_rounds", 10)
    )

    start_time = time.time()
    try:
        if backend["type"] == "gemini":
            text, truncated, tool_calls = _run_gemini_loop(
                backend, max_tokens, max_tool_rounds, prompt, system_prompt
            )
        elif backend["type"] == "vertex-claude":
            text, truncated, tool_calls = _run_vertex_claude_loop(
                backend, max_tokens, max_tool_rounds, prompt, system_prompt
            )
        elif backend["type"] == "openai":
            text, truncated, tool_calls = _run_openai_loop(
                backend, max_tokens, max_tool_rounds, prompt, system_prompt
            )
        else:
            text, truncated, tool_calls = _run_anthropic_loop(
                backend, max_tokens, max_tool_rounds, prompt, system_prompt
            )
        duration_ms = int((time.time() - start_time) * 1000)
    finally:
        # Clear the per-investigation context so later calls on this (possibly
        # pooled) thread don't inherit a stale budget or target server.
        # ContextVars are NOT reset between APScheduler jobs that reuse a worker
        # thread, so reset both explicitly.
        set_current_budget(None)
        try:
            set_current_server(None)
        except Exception:
            pass

    # One shared parser for severity/sections/confidence/addresses (P1-18) —
    # same call as `_parse_and_store` so we can use the one-shot writer and
    # capture the row id.
    parsed = parse_agent_response(text)
    if not parsed["cleaned"]:
        # Same empty-response guard as `_parse_and_store` (P1-5): this sibling
        # path (used by replay + the webhook investigator, which pass
        # incident_id explicitly) must NOT persist a content-free analysis or
        # link/close an incident against one. Leave the incident as-is and
        # return an unstored result.
        logger.error(
            f"{analysis_type} LLM response was empty after parsing "
            "(max-rounds exhaustion or safety block) — not storing, not linking any incident"
        )
        return {
            "text": text, "analysis_id": None, "severity": None,
            "confidence": None, "truncated": truncated,
        }

    severity = parsed["severity"]
    findings = parsed["findings"]
    recommendations = parsed["recommendations"]
    confidence = parsed["confidence"]
    addressed_incident = _resolve_addressed_incident(
        incident_id, parsed["addresses_incident"], analysis_type, prompt
    )

    try:
        analysis_id, linked = writer.write_agent_analysis_and_link(
            {
                "analyzed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                "server_id": server_id,
                "analysis_type": analysis_type,
                "severity": severity,
                "input_summary": prompt[:2000],
                "findings": json.dumps(findings),
                "recommendations": json.dumps(recommendations),
                "applied": 0,
                "outcome_notes": _build_outcome_notes(confidence, truncated),
                "model": backend["model"],
                "tool_calls": tool_calls,
                "duration_ms": duration_ms,
            },
            addressed_incident,
            server_id,
        )
        if addressed_incident is not None and not linked:
            logger.info(
                f"Analysis {analysis_id} not linked to incident {addressed_incident} "
                "(incident is not open on this server)"
            )
    except Exception as e:
        logger.warning(f"Failed to persist {analysis_type} analysis: {e}")
        analysis_id = None

    return {
        "text": text, "analysis_id": analysis_id, "severity": severity,
        "confidence": confidence, "truncated": truncated,
        "model": backend["model"], "tool_calls": tool_calls, "duration_ms": duration_ms,
    }
