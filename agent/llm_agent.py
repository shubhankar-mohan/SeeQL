"""
LLM DBA Agent — MySQL analysis powered by Gemini (Vertex AI) or Claude.

Receives a Structured State Report, reasons about it using tool calls,
and produces findings + recommendations stored in agent_analyses.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

from agent.state_builder import build_state_report
from agent.tools import TOOL_DEFINITIONS, execute_tool
from agent.prompts import SYSTEM_PROMPT, ROUTINE_ANALYSIS_PROMPT, INCIDENT_ANALYSIS_PROMPT, INCIDENT_TRIGGERS
from config import get_config
from storage import writer

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


def run_analysis(analysis_type: str = "routine", trigger_type: str | None = None,
                  server_id: str | None = None) -> dict | None:
    """
    Run a full LLM analysis cycle.

    Args:
        analysis_type: "routine" (scheduled) or "incident" (triggered by alert)
        trigger_type: For incidents, the alert rule name (e.g. "lock_cascade", "high_cpu").
                      Selects trigger-specific instructions in the incident prompt.
        server_id: Which server to analyze. None = default server.

    Returns:
        Analysis result dict, or None if skipped.
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

    max_tokens = config.get("max_tokens", 8192)
    max_tool_rounds = config.get("max_tool_rounds", 15)

    # Build the state report for this server
    report = build_state_report(server_id=server_id)
    state_md = report.to_markdown()

    # Skip quiet periods if configured (only for routine, never for incidents)
    if analysis_type == "routine" and config.get("skip_quiet", True) and _is_quiet(report):
        logger.info("State is quiet, skipping analysis")
        set_current_server(None)
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
        set_current_server(None)
        return None

    logger.info(f"Running {analysis_type} analysis with {backend['type']} ({backend['model']})")

    try:
        if backend["type"] == "gemini":
            result = _run_gemini_loop(backend, max_tokens, max_tool_rounds, user_msg)
        elif backend["type"] == "vertex-claude":
            result = _run_vertex_claude_loop(backend, max_tokens, max_tool_rounds, user_msg)
        elif backend["type"] == "openai":
            result = _run_openai_loop(backend, max_tokens, max_tool_rounds, user_msg)
        else:
            result = _run_anthropic_loop(backend, max_tokens, max_tool_rounds, user_msg)
    except Exception as e:
        logger.error(f"Agent analysis failed: {e}")
        return None
    finally:
        # Symmetric with run_llm_analysis: reset the target-server ContextVar so
        # a pooled worker thread can't leak it into a later call.
        set_current_server(None)

    # Parse and store the result
    analysis = _parse_and_store(result, analysis_type, state_md, server_id)
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
    model = config.get("model", "gemini-2.0-flash")
    has_gcp_creds = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and project_id)

    anthropic_key = _cfg_value(config, "anthropic_api_key")
    openai_key = _cfg_value(config, "openai_api_key") or os.environ.get("OPENAI_API_KEY")
    openai_base_url = _cfg_value(config, "openai_base_url") or os.environ.get("OPENAI_BASE_URL")

    def _vertex_claude():
        return {"type": "vertex-claude", "model": model, "project_id": project_id,
                "region": gcp_config.get("vertex_region", "us-east5")}

    def _anthropic():
        return {"type": "anthropic", "model": model, "api_key": anthropic_key}

    def _gemini(m):
        return {"type": "gemini", "model": m, "project_id": project_id,
                "region": gcp_config.get("vertex_region", "us-central1")}

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
            return _gemini(model if model.startswith("gemini") else "gemini-2.0-flash") \
                if has_gcp_creds else _missing("GCP credentials")
        logger.warning("Unknown agent.provider %r; falling back to auto-detection.", provider)

    # --- Model-name-driven detection ------------------------------------------
    if _looks_like_openai(model) and (openai_key or openai_base_url):
        return _openai()
    if model.startswith("claude"):
        if has_gcp_creds:
            return _vertex_claude()
        if anthropic_key:
            return _anthropic()
    if model.startswith("gemini") and has_gcp_creds:
        return _gemini(model)

    # --- Fallback by whatever credentials exist -------------------------------
    if has_gcp_creds:
        if not model.startswith("gemini"):
            logger.warning("Model %r has no matching backend/creds; using gemini-2.0-flash "
                           "via Vertex AI (GCP creds present).", model)
            model = "gemini-2.0-flash"
        return _gemini(model)
    if anthropic_key:
        if not model.startswith("claude"):
            logger.warning("Model %r has no matching backend/creds; using "
                           "claude-sonnet-4-20250514 via the Anthropic API.", model)
            model = "claude-sonnet-4-20250514"
        return _anthropic()
    if openai_key or openai_base_url:
        return _openai()

    logger.warning("No LLM credentials configured (need GOOGLE_APPLICATION_CREDENTIALS, "
                   "ANTHROPIC_API_KEY, or OPENAI_API_KEY / openai_base_url).")
    return None


def _missing(what: str) -> None:
    logger.warning("Configured agent.provider requires %s, which is not available.", what)
    return None


def _run_gemini_loop(backend: dict, max_tokens: int, max_rounds: int, user_msg: str) -> str:
    """Run tool-use loop with Gemini via Vertex AI."""
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

    final_text = ""
    total_tool_calls = 0
    for round_num in range(max_rounds):
        logger.info(f"  Agent round {round_num + 1}/{max_rounds}")
        response = client.models.generate_content(
            model=backend["model"],
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[tools],
                max_output_tokens=max_tokens,
                temperature=0,
            ),
        )

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
            tool_response_parts.append(
                types.Part.from_function_response(name=name, response={"result": result})
            )

        contents.append(types.Content(role="user", parts=tool_response_parts))
    else:
        logger.warning(f"  Agent hit max rounds ({max_rounds}) with {total_tool_calls} tool calls")

    return final_text


def _run_vertex_claude_loop(backend: dict, max_tokens: int, max_rounds: int, user_msg: str) -> str:
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
    return _run_claude_loop(client, backend["model"], max_tokens, max_rounds, user_msg)


def _run_anthropic_loop(backend: dict, max_tokens: int, max_rounds: int, user_msg: str) -> str:
    """Run tool-use loop with Claude via Anthropic API."""
    import anthropic
    client = anthropic.Anthropic(api_key=backend["api_key"])
    return _run_claude_loop(client, backend["model"], max_tokens, max_rounds, user_msg)


def _run_openai_loop(backend: dict, max_tokens: int, max_rounds: int, user_msg: str) -> str:
    """Tool-use loop for OpenAI and any OpenAI-compatible endpoint.

    A `base_url` in the backend points the OpenAI SDK at a compatible server
    (Azure OpenAI, Ollama, vLLM, Groq, OpenRouter, LM Studio, …), so this single
    loop covers "OpenAI" and "bring your own / any other LLM".
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

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    final_text = ""
    total_tool_calls = 0

    for round_num in range(max_rounds):
        logger.info(f"  Agent round {round_num + 1}/{max_rounds}")
        response = client.chat.completions.create(
            model=backend["model"],
            max_tokens=max_tokens,
            tools=OPENAI_TOOL_DEFINITIONS,
            messages=messages,
            temperature=0,
        )
        choices = response.choices or []
        if not choices:
            logger.warning("OpenAI-compatible endpoint returned no choices; ending tool loop")
            break
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
        logger.warning(f"  Agent hit max rounds ({max_rounds}) with {total_tool_calls} tool calls")

    return final_text


def _run_claude_loop(client, model: str, max_tokens: int, max_rounds: int, user_msg: str) -> str:
    """Shared tool-use loop for any Claude client (API or Vertex)."""
    messages = [{"role": "user", "content": user_msg}]
    final_text = ""
    total_tool_calls = 0

    for round_num in range(max_rounds):
        logger.info(f"  Agent round {round_num + 1}/{max_rounds}")
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
            temperature=0,
        )

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
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})
    else:
        logger.warning(f"  Agent hit max rounds ({max_rounds}) with {total_tool_calls} tool calls")

    return final_text


def _is_quiet(report) -> bool:
    """Check if the state report has nothing interesting to analyze."""
    changes = report.changes
    cs = report.current_state

    # Not quiet if there are regressions, DDL changes, deadlocks, or locks
    if changes.get("regressions"):
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


def _parse_and_store(text: str, analysis_type: str, input_summary: str,
                     server_id: str = "default") -> dict:
    """Parse agent response and store in agent_analyses table."""
    # Strip code fences — Gemini sometimes wraps its output in ```markdown blocks
    cleaned = re.sub(r'^```(?:markdown)?\s*\n?', '', text, flags=re.MULTILINE)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()

    # Extract severity — only match the header line, not mentions in body text.
    severity = "info"
    sev_match = re.search(
        r'^#{2,3}\s*Severity:\s*(critical|warning|info)',
        cleaned, re.IGNORECASE | re.MULTILINE,
    )
    if sev_match:
        severity = sev_match.group(1).lower()

    # Extract findings and recommendations sections (case-insensitive, flexible markdown)
    findings = _extract_section(cleaned, "findings", "recommendations")
    recommendations = _extract_section(cleaned, "recommendations", None)

    # If both are empty, store the full response so nothing is lost
    if not findings and not recommendations:
        findings = cleaned
        logger.warning("Could not parse sections from agent response, storing full text as findings")

    analysis = {
        "analyzed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "server_id": server_id,
        "analysis_type": analysis_type,
        "severity": severity,
        "input_summary": input_summary[:2000],
        "findings": json.dumps(findings),
        "recommendations": json.dumps(recommendations),
        "applied": 0,
        "outcome_notes": None,
    }

    try:
        writer.write_agent_analysis([analysis])
        logger.info(f"Stored {analysis_type} analysis (severity={severity})")
    except Exception as e:
        logger.error(f"Failed to store analysis: {e}")

    analysis["raw_response"] = text
    return analysis


# Regex to find markdown section headers like "### Findings", "## FINDINGS", "**Findings**"
_SECTION_RE_CACHE = {}

def _section_pattern(name: str) -> re.Pattern:
    if name not in _SECTION_RE_CACHE:
        # Note: use string concatenation, not f-string — f-string mangles {2,3} quantifiers
        _SECTION_RE_CACHE[name] = re.compile(
            r'^(?:#{2,3}\s*|\*\*)' + name + r'(?:\*\*)?[:\s]*$',
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
) -> dict:
    """
    Public wrapper that dispatches any custom prompt to the configured LLM
    backend, stores the result in `agent_analyses`, and returns
    `{"text": str, "analysis_id": int}`.

    Used by `agent.replay.run_replay` so the replay module doesn't have to
    reimplement backend detection or row-id wiring. Raises RuntimeError if
    no LLM backend is configured — callers should catch and fall back to
    the timeline-only rendering.

    The webhook investigator passes `tool_budget` (an `alerting.budget.Budget`)
    and a lower `max_tool_rounds_override` so Phase 2 stays bounded.
    """
    config = get_config().get("agent", {})
    backend = _detect_backend(config)
    if backend is None:
        raise RuntimeError("No LLM backend configured")

    if server_id is None:
        from config.server_registry import get_server_registry
        server_id = get_server_registry().get_default_server_id()

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

    try:
        if backend["type"] == "gemini":
            text = _run_gemini_loop(backend, max_tokens, max_tool_rounds, prompt)
        elif backend["type"] == "vertex-claude":
            text = _run_vertex_claude_loop(backend, max_tokens, max_tool_rounds, prompt)
        elif backend["type"] == "openai":
            text = _run_openai_loop(backend, max_tokens, max_tool_rounds, prompt)
        else:
            text = _run_anthropic_loop(backend, max_tokens, max_tool_rounds, prompt)
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

    # Parse severity + sections locally (same logic as _parse_and_store) so
    # we can use the one-shot writer and capture the row id.
    cleaned = re.sub(r'^```(?:markdown)?\s*\n?', '', text, flags=re.MULTILINE)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE).strip()

    severity = "info"
    m = re.search(
        r'^#{2,3}\s*Severity:\s*(critical|warning|info)',
        cleaned, re.IGNORECASE | re.MULTILINE,
    )
    if m:
        severity = m.group(1).lower()

    findings, recommendations = _split_findings_recommendations(cleaned)

    try:
        analysis_id = writer.write_agent_analysis_one({
            "analyzed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "server_id": server_id,
            "analysis_type": analysis_type,
            "severity": severity,
            "input_summary": prompt[:2000],
            "findings": json.dumps(findings),
            "recommendations": json.dumps(recommendations),
            "applied": 0,
            "outcome_notes": None,
        })
    except Exception as e:
        logger.warning(f"Failed to persist {analysis_type} analysis: {e}")
        analysis_id = None

    return {"text": text, "analysis_id": analysis_id, "severity": severity}
