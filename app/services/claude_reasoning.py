"""
claude_reasoning.py
Claude-powered reasoning layer for the Sales Operating System.

Adds natural-language explanation and narrative generation on top of the
rules-based recommendation engine. The engine scores; Claude explains.

Required env var:
    ANTHROPIC_API_KEY — Anthropic API key

If not configured, all functions return graceful fallbacks so the
deterministic rules engine continues to work without Claude.

Runtime governance (Gigaton Canonical First Principles §6): all LLM calls
flow through `_call`, which carries provider, model, prompt_version, and
schema_version, and emits an audit log line per invocation.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

try:
    import anthropic
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

PROVIDER = "anthropic"
MODEL = "claude-opus-4-6"
PROMPT_VERSION_EXPLAIN = "explain_recommendations.v1.0"
SCHEMA_VERSION_EXPLAIN = "coaching_prose.v1"
PROMPT_VERSION_PROPOSAL = "draft_proposal.v1.0"
SCHEMA_VERSION_PROPOSAL = "proposal_markdown.v1"
PROMPT_VERSION_NEEDS = "detect_need_states.v1.0"
SCHEMA_VERSION_NEEDS = "matched_ids.v1"

_client: "anthropic.Anthropic | None" = None
_audit_log = logging.getLogger("sales_os.llm_audit")


def is_available() -> bool:
    return _AVAILABLE and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client() -> "anthropic.Anthropic":
    global _client
    if not _AVAILABLE:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for Claude reasoning")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _call(
    prompt: str,
    *,
    prompt_version: str,
    schema_version: str,
    max_tokens: int = 1024,
    provider: str = PROVIDER,
    model: str = MODEL,
) -> str:
    """Provider-agnostic LLM invocation with mandatory audit envelope."""
    if provider != "anthropic":
        raise NotImplementedError(f"Provider {provider!r} not wired yet")
    client = _get_client()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text if message.content else ""
    _audit_log.info(
        "llm_call provider=%s model=%s prompt_version=%s schema_version=%s "
        "in_chars=%d out_chars=%d",
        provider, model, prompt_version, schema_version,
        len(prompt), len(text),
    )
    return text


def _parse_json(text: str) -> dict:
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Recommendation explanation
# ---------------------------------------------------------------------------

def explain_recommendations(
    opportunity: dict[str, Any],
    recommendations: list[dict[str, Any]],
) -> str:
    """
    Generate a human-readable explanation of why these recommendations
    were surfaced for this opportunity.

    Returns a markdown string. Falls back to a plain summary if Claude
    is not available.
    """
    if not is_available() or not recommendations:
        return _fallback_explanation(recommendations)

    rec_list = "\n".join(
        f"- {r.get('product_name', 'Unknown')} ({r.get('recommendation_type', '?')}, "
        f"confidence={r.get('confidence_score', 0):.0%})"
        for r in recommendations[:10]
    )

    prompt = f"""You are a B2B sales coach explaining product recommendations to a sales rep.

OPPORTUNITY CONTEXT:
{json.dumps(opportunity, indent=2)}

RECOMMENDED PRODUCTS (from rules engine):
{rec_list}

Write a concise coaching note (2-3 sentences) explaining:
1. Why these specific products fit this opportunity
2. What the rep should lead with
3. Any risk or objection to anticipate

Be specific, practical, and direct. No bullet points — prose only."""

    return _call(
        prompt,
        prompt_version=PROMPT_VERSION_EXPLAIN,
        schema_version=SCHEMA_VERSION_EXPLAIN,
        max_tokens=512,
    )


def _fallback_explanation(recommendations: list[dict[str, Any]]) -> str:
    if not recommendations:
        return "No recommendations available for this opportunity."
    names = [r.get("product_name", "Unknown") for r in recommendations[:5]]
    return f"Recommended: {', '.join(names)}."


# ---------------------------------------------------------------------------
# Proposal draft generation
# ---------------------------------------------------------------------------

def draft_proposal(
    opportunity: dict[str, Any],
    recommendations: list[dict[str, Any]],
    catalog_items: list[dict[str, Any]],
) -> str:
    """
    Generate a full proposal draft in markdown.
    Falls back to a structured plain-text summary if Claude is unavailable.
    """
    if not is_available():
        return _fallback_proposal(opportunity, recommendations)

    rec_details = "\n".join(
        f"- **{r.get('product_name')}** — {r.get('recommendation_type')} "
        f"(confidence {r.get('confidence_score', 0):.0%})"
        for r in recommendations[:10]
    )

    prompt = f"""You are a senior account executive writing a proposal for a B2B marketing services company.

CLIENT: {opportunity.get('client_name', 'Client')}
OPPORTUNITY: {opportunity.get('name', 'Untitled')}
STAGE: {opportunity.get('stage', 'unknown')}
DEAL SIZE: ${opportunity.get('deal_size', 0):,.0f}

RECOMMENDED SERVICES:
{rec_details}

Write a professional proposal in markdown with these sections:
## Executive Summary
## The Challenge
## Our Recommended Solution
## Why Now
## Next Steps

Guidelines:
- Focus on business outcomes, not features
- Reference specific recommended services by name
- Keep each section to 2-3 sentences
- Professional, confident tone

Return only the markdown."""

    return _call(
        prompt,
        prompt_version=PROMPT_VERSION_PROPOSAL,
        schema_version=SCHEMA_VERSION_PROPOSAL,
        max_tokens=1500,
    )


def _fallback_proposal(
    opportunity: dict[str, Any],
    recommendations: list[dict[str, Any]],
) -> str:
    names = [r.get("product_name", "Unknown") for r in recommendations[:5]]
    return (
        f"# Proposal: {opportunity.get('name', 'Untitled')}\n\n"
        f"**Client:** {opportunity.get('client_name', 'Unknown')}\n\n"
        f"**Recommended services:** {', '.join(names) if names else 'None'}\n"
    )


# ---------------------------------------------------------------------------
# Need state detection from free-form text
# ---------------------------------------------------------------------------

def detect_need_states(
    conversation_text: str,
    known_need_states: list[dict[str, Any]],
) -> list[str]:
    """
    Use Claude to detect which known need states are present in a conversation
    or discovery call transcript.

    Returns list of matched need_state_ids. Falls back to [] if unavailable.
    """
    if not is_available() or not conversation_text.strip():
        return []

    ns_list = "\n".join(
        f"- ID={ns.get('id')} | Problem: {ns.get('problem_name')} | Signal: {ns.get('detected_signal', '')}"
        for ns in known_need_states[:20]
    )

    prompt = f"""You are a sales intelligence system analyzing a conversation for client pain points.

KNOWN NEED STATES:
{ns_list}

CONVERSATION TEXT:
{conversation_text[:3000]}

Identify which need state IDs are clearly evident in this conversation.
Return ONLY valid JSON:
{{"matched_ids": ["id1", "id2"]}}

Only include IDs where the evidence is strong and explicit."""

    text = _call(
        prompt,
        prompt_version=PROMPT_VERSION_NEEDS,
        schema_version=SCHEMA_VERSION_NEEDS,
        max_tokens=256,
    )
    result = _parse_json(text)
    return result.get("matched_ids", [])
