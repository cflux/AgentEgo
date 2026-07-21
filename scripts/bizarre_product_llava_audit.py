#!/usr/bin/env python3
"""Optional, fail-soft independent LLaVA audit for Bizarre Product commentary.

The audit never changes the delivery payload in soft mode. Its response is parsed
strictly and the final decision is computed from validated claim statuses rather
than the model's advisory ``approved`` field.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "llava:13b"
DEFAULT_TIMEOUT = 180
STATUSES = frozenset({"supported", "metaphor", "unsupported", "uncertain"})
MAX_CLAIM_CHARS = 520
MAX_REASON_CHARS = 320


@dataclass(frozen=True)
class AuditResult:
    decision: str
    statuses: tuple[str, ...] = ()
    claim_count: int = 0
    reason: str = ""
    latency_ms: int | None = None


def _bounded_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is not a string")
    text = value.strip()
    if not text or len(text) > limit or "\n" in text or "\r" in text:
        raise ValueError(f"{field} is empty, multiline, or too long")
    if "http://" in text.lower() or "https://" in text.lower() or "@everyone" in text.lower() or "@here" in text.lower():
        raise ValueError(f"{field} contains a link or mention")
    return text


def parse_audit_content(content: str) -> tuple[tuple[dict[str, str], ...], str]:
    """Parse only the exact JSON object emitted by the verifier."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty verifier content")
    if content != content.strip() or "```" in content:
        raise ValueError("markdown or surrounding prose is not allowed")
    value = json.loads(content)
    if not isinstance(value, dict) or set(value) != {"approved", "claims", "reason"}:
        raise ValueError("top-level schema mismatch")
    if not isinstance(value["approved"], bool):
        raise ValueError("approved is not boolean")
    reason = _bounded_text(value["reason"], "reason", MAX_REASON_CHARS)
    claims = value["claims"]
    if not isinstance(claims, list) or not claims:
        raise ValueError("claims must be a non-empty list")
    parsed: list[dict[str, str]] = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"claim", "status", "reason"}:
            raise ValueError("claim schema mismatch")
        parsed.append(
            {
                "claim": _bounded_text(claim["claim"], "claim", MAX_CLAIM_CHARS),
                "status": claim["status"] if isinstance(claim["status"], str) else "",
                "reason": _bounded_text(claim["reason"], "claim reason", MAX_REASON_CHARS),
            }
        )
        if parsed[-1]["status"] not in STATUSES:
            raise ValueError("invalid claim status")
    return tuple(parsed), reason


def deterministic_decision(claims: tuple[dict[str, str], ...]) -> str:
    statuses = {claim["status"] for claim in claims}
    if statuses.intersection({"unsupported", "uncertain"}):
        return "reject"
    return "approve"


def _request_body(image_b64: str, analysis: str, commentary: str, model: str) -> dict:
    prompt = (
        "You are an independent visual grounding verifier. Inspect the attached image yourself, "
        "then audit the proposed commentary against visible pixels. Return exactly one JSON object "
        "with keys approved, claims, reason. Each claims item must have exactly claim, status, reason. "
        "Statuses are exactly supported, metaphor, unsupported, or uncertain. "
        "Mark visible concrete claims supported; obvious jokes, similes, and personification metaphor; "
        "concrete claims absent from the image unsupported; ambiguous claims uncertain. "
        "The approved field is advisory and will be ignored by the caller. Do not rewrite the commentary. "
        "Do not mention source, URL, filename, or tools. JSON only, no markdown.\n\n"
        f"SALT IMAGE_ANALYSIS: {analysis}\nSALT COMMENTARY: {commentary}"
    )
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
        "stream": False,
        "format": {
            "type": "object",
            "additionalProperties": False,
            "required": ["approved", "claims", "reason"],
            "properties": {
                "approved": {"type": "boolean"},
                "reason": {"type": "string", "minLength": 1},
                "claims": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["claim", "status", "reason"],
                        "properties": {
                            "claim": {"type": "string"},
                            "status": {"type": "string", "enum": sorted(STATUSES)},
                            "reason": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
        "options": {"temperature": 0, "num_predict": 450},
    }


def audit_commentary(
    image_path: str,
    image_sha256: str,
    analysis: str,
    commentary: str,
    *,
    opener: Callable | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
) -> AuditResult:
    """Run one strict audit; all operational/schema errors become unavailable."""
    try:
        data = Path(image_path).read_bytes()
        if hashlib.sha256(data).hexdigest() != image_sha256:
            raise ValueError("image hash changed before audit")
        image_b64 = base64.b64encode(data).decode("ascii")
        request = Request(
            endpoint or os.environ.get("BIZARRE_PRODUCT_LLAVA_ENDPOINT", DEFAULT_ENDPOINT),
            data=json.dumps(_request_body(image_b64, analysis, commentary, model or os.environ.get("BIZARRE_PRODUCT_LLAVA_MODEL", DEFAULT_MODEL)), separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with (opener or urlopen)(request, timeout=timeout or int(os.environ.get("BIZARRE_PRODUCT_LLAVA_TIMEOUT", DEFAULT_TIMEOUT))) as response:
            envelope = json.loads(response.read().decode("utf-8"))
        content = envelope["message"]["content"]
        claims, reason = parse_audit_content(content)
        statuses = tuple(claim["status"] for claim in claims)
        return AuditResult(deterministic_decision(claims), statuses, len(claims), reason)
    except (OSError, HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        return AuditResult("unavailable", reason=f"{type(error).__name__}: {error}")
