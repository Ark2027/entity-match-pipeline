"""Ask a language model to resolve the pairs the scorer declined to.

The deterministic matcher is good at names that differ mechanically: a legal
suffix, a DBA, transposed words. It is bad at cases needing knowledge it does
not have. Whether "NW Ironwood Supply Co" is "Northwest Ironwood Supply" or
"Northwest Iron Works" is not a string distance question.

This module only ever sees records the scorer already declined to resolve. It
cannot touch an auto-accept and it cannot resurrect a discard. That constraint
is the whole design: a model that can only act on ambiguous cases cannot degrade
the confident ones.

Guardrails, in the order they matter:

1. Scope. Only the review and possible-match bands are eligible.
2. Grounding. A returned id must be one that was actually offered. Anything else
   is treated as a refusal, not a match.
3. Confidence. Low confidence stays deferred rather than resolving.
4. Budget. Hard caps on rows per run and on retries.
5. Scrubbing. Errors are stripped of credentials before they are logged or
   written anywhere, because that is how keys usually escape.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-sonnet-5"
ELIGIBLE_BANDS = frozenset({"review", "possible_match"})
MAX_CANDIDATES_SHOWN = 5

# The schema deliberately has ONE field carrying the decision.
#
# The first version had both a "decision" enum and a nullable id, which encode
# the same fact. The model dropped the redundant enum and returned only the id;
# the parser read the missing key as a refusal, so every single adjudication
# came back "no_match" while the reasoning field argued the opposite. The eval
# caught it, the reasoning strings diagnosed it.
#
# Two lessons kept here on purpose: redundant fields in structured output invite
# silent contradiction, and a parser must treat a missing field as an error
# rather than quietly falling through to a default.
ADJUDICATION_TOOL = {
    "name": "record_decision",
    "description": (
        "Record which candidate is the same business as the source record. "
        "Set matched_candidate_id to the chosen candidate's id, or to 0 if none of them is the same business."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "matched_candidate_id": {
                "type": "integer",
                "description": (
                    "The id of the candidate that is the same business. "
                    "Use 0 when none of the candidates is the same business."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "How certain the decision is. Use low when the evidence is genuinely thin.",
            },
            "reasoning": {
                "type": "string",
                "description": "One or two sentences naming the evidence that decided it.",
            },
        },
        "required": ["matched_candidate_id", "confidence", "reasoning"],
    },
}

NO_MATCH_ID = 0

SYSTEM_PROMPT = """You decide whether two records describe the same real-world business.

You are seeing only the cases an automated scorer could not settle. It already \
handles obvious rewordings, so if a pair looks trivially identical, look again \
for the reason it was flagged.

Judge on the whole picture, not name similarity alone:

- A holding company and its trading name are usually the same business.
- Abbreviations and expansions are common ("NW" for "Northwest").
- Words like Group, Services, Holdings, Capital and Partners carry almost no \
identifying information. Two names sharing only those words are usually \
different businesses.
- A shared distinctive word matters far more than a high overall similarity.
- Matching postal code, an amount inside the stated size band, and a plausible \
date order are corroborating evidence, not proof.

Returning 0 for no match is a correct and useful answer. A wrong match is worse \
than no answer, because it enters downstream systems unchallenged while a \
deferral only costs a person a minute. When the evidence is thin, say so with \
low confidence rather than guessing."""


def scrub(text: str) -> str:
    """Remove anything credential-shaped from text before it is logged.

    API errors are a common way for keys to end up in log aggregators.
    """
    text = re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "[redacted-api-key]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"(x-api-key['\"=:\s]+)[A-Za-z0-9._\-]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    return text[:600]


@dataclass(frozen=True)
class Candidate:
    application_id: int
    business_name: str
    state: str = ""
    postal_code: str = ""
    size_band: str = ""
    lead_date: str = ""
    name_score: float | None = None

    def describe(self) -> str:
        bits = [f"id={self.application_id}", f'name="{self.business_name}"']
        if self.state:
            bits.append(f"state={self.state}")
        if self.postal_code:
            bits.append(f"postal={self.postal_code}")
        if self.size_band:
            bits.append(f"size_band={self.size_band}")
        if self.lead_date:
            bits.append(f"lead_created={self.lead_date}")
        if self.name_score is not None:
            bits.append(f"name_similarity={self.name_score:.0f}")
        return "  - " + ", ".join(bits)


@dataclass(frozen=True)
class Request:
    live_business_name: str
    state: str
    source_name: str
    candidates: tuple[Candidate, ...]
    postal_code: str = ""
    amount: float | None = None
    origination_date: str = ""

    def prompt(self) -> str:
        lines = [
            "Source record:",
            f'  name="{self.live_business_name}"',
            f"  state={self.state}",
            f"  reported_by={self.source_name}",
        ]
        if self.postal_code:
            lines.append(f"  postal={self.postal_code}")
        if self.amount is not None:
            lines.append(f"  amount={self.amount:,.0f}")
        if self.origination_date:
            lines.append(f"  dated={self.origination_date}")
        lines.append("")
        lines.append(f"Candidates ({len(self.candidates)}):")
        lines.extend(c.describe() for c in self.candidates[:MAX_CANDIDATES_SHOWN])
        lines.append("")
        lines.append("Which candidate, if any, is the same business? Record your decision.")
        return "\n".join(lines)


@dataclass
class Decision:
    resolved: bool
    application_id: int | None = None
    confidence: str = "low"
    reasoning: str = ""
    refused_reason: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error


class Model(Protocol):
    """Anything that can answer an adjudication. Lets tests run without network."""

    def adjudicate(self, request: Request) -> Decision: ...


@dataclass
class StubModel:
    """Deterministic stand-in used by tests and by runs with no API key."""

    always: str = "no_match"
    calls: list[Request] = field(default_factory=list)

    def adjudicate(self, request: Request) -> Decision:
        self.calls.append(request)
        if self.always == "top" and request.candidates:
            return Decision(resolved=True, application_id=request.candidates[0].application_id,
                            confidence="high", reasoning="stub: took the first candidate")
        return Decision(resolved=False, confidence="high", reasoning="stub: declined")


class ClaudeModel:
    """Calls the Anthropic API with a schema-constrained tool.

    The key is read from the environment and never stored on the instance, so it
    cannot end up in a repr, a pickle, or a traceback.
    """

    def __init__(self, model: str = DEFAULT_MODEL, max_retries: int = 2, timeout: float = 30.0) -> None:
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self._client = anthropic.Anthropic(max_retries=max_retries, timeout=timeout)
        self.model = model

    def adjudicate(self, request: Request) -> Decision:
        started = time.perf_counter()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=400,
                system=SYSTEM_PROMPT,
                tools=[ADJUDICATION_TOOL],
                tool_choice={"type": "tool", "name": "record_decision"},
                messages=[{"role": "user", "content": request.prompt()}],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a scrubbed string
            return Decision(resolved=False, error=f"{type(exc).__name__}: {scrub(str(exc))}",
                            latency_ms=int((time.perf_counter() - started) * 1000))

        latency = int((time.perf_counter() - started) * 1000)
        payload = _tool_payload(response)
        if payload is None:
            return Decision(resolved=False, error="model returned no tool call", latency_ms=latency)

        usage = getattr(response, "usage", None)
        decision = _from_payload(payload, request)
        decision.latency_ms = latency
        decision.input_tokens = getattr(usage, "input_tokens", 0) or 0
        decision.output_tokens = getattr(usage, "output_tokens", 0) or 0
        return decision


def _tool_payload(response: Any) -> dict | None:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "record_decision":
            return dict(getattr(block, "input", {}) or {})
    return None


def _from_payload(payload: dict, request: Request) -> Decision:
    """Turn a tool payload into a Decision, refusing anything ungrounded."""
    reasoning = str(payload.get("reasoning") or "")[:400]
    confidence = str(payload.get("confidence") or "low").lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    # A missing required field is a malformed response, not a refusal. Saying so
    # loudly is the difference between finding this in ten minutes and shipping a
    # system that silently declines everything.
    if "matched_candidate_id" not in payload:
        return Decision(resolved=False, confidence=confidence, reasoning=reasoning,
                        error="response omitted matched_candidate_id")

    try:
        chosen = int(payload["matched_candidate_id"])
    except (TypeError, ValueError):
        return Decision(resolved=False, confidence=confidence, reasoning=reasoning,
                        error=f"matched_candidate_id was not an integer: {payload['matched_candidate_id']!r}")

    if chosen == NO_MATCH_ID:
        return Decision(resolved=False, confidence=confidence, reasoning=reasoning,
                        refused_reason="model found no matching candidate")

    # Grounding check. An id that was never offered is a hallucination, and
    # treating it as a match would be worse than declining.
    offered = {c.application_id for c in request.candidates}
    if chosen not in offered:
        return Decision(resolved=False, confidence=confidence, reasoning=reasoning,
                        refused_reason=f"id {chosen} was not among the candidates offered")

    if confidence == "low":
        return Decision(resolved=False, application_id=chosen, confidence=confidence,
                        reasoning=reasoning, refused_reason="confidence too low to resolve")

    return Decision(resolved=True, application_id=chosen, confidence=confidence, reasoning=reasoning)


def adjudicate_all(model: Model, requests: list[Request], max_rows: int = 200) -> list[Decision]:
    """Adjudicate a batch, stopping at the row cap."""
    return [model.adjudicate(r) for r in requests[:max_rows]]
