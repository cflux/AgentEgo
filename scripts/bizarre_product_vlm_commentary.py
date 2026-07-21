#!/usr/bin/env python3
"""Enrich a verified Bizarre Product collector payload with Salt VLM evidence.

Read exactly one collector JSON object from stdin. Success writes one strict JSON
object; every failure writes exactly [SILENT] to stdout and diagnostics to stderr.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VLM_ENDPOINT = "http://salt-gx10.local:8000/v1/chat/completions"
VLM_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct-AWQ"
VLM_TIMEOUT = 180
CEDAR_ENDPOINT = "http://cedar-gx10.local:11434/v1/chat/completions"
CEDAR_MODEL = "qwen2.5:72b"
CEDAR_TIMEOUT = 90
MAX_ANALYSIS_CHARS = 320
MAX_COMMENTARY_CHARS = 520
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"@(?:everyone|here|[A-Za-z0-9_]{2,})", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-z]{4,}")
FORBIDDEN_COMMENTARY_RE = re.compile(
    r"\b(?:listing|seller|source|manufacturer|retailer|price|shipping)\b", re.IGNORECASE
)
STOPWORDS = frozenset(
    {
        "that", "this", "with", "from", "into", "beside", "there", "their",
        "where", "which", "looks", "look", "like", "installed", "standing",
        "stands", "image", "photo", "picture", "visible", "shown", "appears",
        "hotel", "water", "fountain", "wall", "side", "setup", "drinking",
    }
)


class SilentFailure(RuntimeError):
    """A delivery-blocking validation failure."""


def log(message: str) -> None:
    print(f"[bizarre-product-vlm] {message}", file=sys.stderr)


def load_module(filename: str, module_name: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SilentFailure("cannot load collector validation module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    script_dir = str(path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec.loader.exec_module(module)
    return module


def load_collector_module():
    return load_module("bizarre_product_collector.py", "bizarre_product_collector")


def load_llava_audit_module():
    return load_module("bizarre_product_llava_audit.py", "bizarre_product_llava_audit")


def run_optional_llava_audit(image_path: str, image_sha256: str, analysis: str, commentary: str) -> None:
    if os.environ.get("BIZARRE_PRODUCT_LLAVA_AUDIT", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        audit = load_llava_audit_module().audit_commentary(
            image_path, image_sha256, analysis, commentary
        )
        statuses = ",".join(audit.statuses) or "none"
        log(f"llava_audit decision={audit.decision} claims={audit.claim_count} statuses={statuses} reason={audit.reason[:180]}")
    except Exception as error:
        # Soft-audit mode must never suppress the already validated delivery.
        log(f"llava_audit decision=unavailable reason={type(error).__name__}: {error}")


def image_bytes_and_mime(image_path: str) -> tuple[bytes, str]:
    image = Path(image_path)
    try:
        data = image.read_bytes()
    except OSError as error:
        raise SilentFailure(f"cannot read verified image: {error}") from error
    if data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png"
    raise SilentFailure("image signature changed or is unsupported")


def validate_text(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise SilentFailure(f"{field} is not a string")
    text = value.strip()
    if not text or len(text) > max_chars or "\n" in text or "\r" in text:
        raise SilentFailure(f"{field} is empty, multiline, or too long")
    if URL_RE.search(text) or MENTION_RE.search(text) or "[" in text or "]" in text:
        raise SilentFailure(f"{field} contains a link, mention, or markup")
    if text.lower().startswith(("i cannot", "i can't", "as an ai", "tool:")):
        raise SilentFailure(f"{field} contains model/tool narration")
    return text


def parse_model_object(raw: bytes, expected_key: str, max_chars: int) -> str:
    try:
        envelope = json.loads(raw.decode("utf-8"))
        content = envelope["choices"][0]["message"]["content"]
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise SilentFailure("VLM response is not the required JSON envelope") from error
    if not isinstance(value, dict) or set(value) != {expected_key}:
        raise SilentFailure("VLM response fields do not match strict schema")
    return validate_text(value[expected_key], expected_key, max_chars)


def model_request(
    endpoint: str,
    body: dict,
    *,
    service: str,
    timeout: int,
    opener: Callable = urlopen,
) -> bytes:
    request = Request(
        endpoint,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            return response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise SilentFailure(f"{service} request failed: {error}") from error


def model_body(content: list[dict], max_tokens: int, model: str) -> dict:
    return {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "system",
                "content": "Return only the exact JSON object requested. Do not use markdown.",
            },
            {"role": "user", "content": content},
        ],
    }


def visual_anchor_tokens(analysis: str) -> set[str]:
    return {
        word.lower()
        for word in WORD_RE.findall(analysis)
        if word.lower() not in STOPWORDS
    }


def validate_commentary(commentary: str, title: str, analysis: str) -> str:
    commentary = validate_text(commentary, "commentary", MAX_COMMENTARY_CHARS)
    if len(commentary) < 40:
        raise SilentFailure("commentary is too short")
    if FORBIDDEN_COMMENTARY_RE.search(commentary):
        raise SilentFailure("commentary makes unsupported source/product claims")
    anchors = visual_anchor_tokens(analysis)
    words = {word.lower() for word in WORD_RE.findall(commentary)}
    if not anchors.intersection(words):
        raise SilentFailure("commentary has no validated visual-evidence anchor")
    return commentary


def enrich(
    collector_result: dict,
    *,
    opener: Callable | None = None,
    salt_opener: Callable | None = None,
    cedar_opener: Callable | None = None,
) -> dict[str, str]:
    """Use Salt only for image facts; Cedar writes the bounded reaction."""
    if opener is not None:
        if salt_opener is not None or cedar_opener is not None:
            raise ValueError("use opener or service-specific openers, not both")
        salt_opener = cedar_opener = opener
    salt_opener = salt_opener or urlopen
    cedar_opener = cedar_opener or urlopen
    collector = load_collector_module()
    try:
        verified = collector.validate_result(collector_result)
    except Exception as error:
        raise SilentFailure(f"collector payload failed validation: {error}") from error

    data, mime = image_bytes_and_mime(verified["image_path"])
    image_sha256 = hashlib.sha256(data).hexdigest()
    data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    analysis_prompt = (
        "Analyze only visible facts in this image. Do not identify a seller, source, "
        "product listing, brand, price, or anything not visually observable. Return exactly "
        "{\"image_analysis\":\"...\"}, one concise sentence."
    )
    analysis = parse_model_object(
        model_request(
            VLM_ENDPOINT,
            model_body(
                [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": analysis_prompt},
                ],
                120,
                VLM_MODEL,
            ),
            service="Salt VLM",
            timeout=VLM_TIMEOUT,
            opener=salt_opener,
        ),
        "image_analysis",
        MAX_ANALYSIS_CHARS,
    )

    # Re-read after the remote call: an external file swap cannot silently change delivery media.
    current_data, _ = image_bytes_and_mime(verified["image_path"])
    if hashlib.sha256(current_data).hexdigest() != image_sha256:
        raise SilentFailure("verified image changed during VLM analysis")

    commentary_prompt = (
        "You are Becca: a manic, chaotic Night City goblin with punchy, irreverent energy. "
        "React like a real person who is delighted by ridiculous DIY chrome; light slang or a "
        "single curse is fine when it fits, but never harass anyone or claim facts you were not given. "
        "Write a specific, playful reaction to the visible setup. Give the joke room to land: "
        "two to four short sentences is a good default, but prioritize natural prose over a formula. "
        "Name at least one concrete visual detail before riffing on it. Do not add facts beyond "
        "TITLE and VISUAL EVIDENCE. Do not repeat the title; it is rendered separately. Do not "
        "mention a source, listing, seller, brand, price, URL, user, or attachment. Return exactly {\"commentary\":\"...\"}.\n"
        f"TITLE CONTEXT: {verified['title']}\nVISUAL EVIDENCE: {analysis}"
    )
    def request_commentary(prompt: str) -> str:
        return parse_model_object(
            model_request(
                CEDAR_ENDPOINT,
                model_body([{"type": "text", "text": prompt}], 180, CEDAR_MODEL),
                service="Cedar commentary",
                timeout=CEDAR_TIMEOUT,
                opener=cedar_opener,
            ),
            "commentary",
            MAX_COMMENTARY_CHARS,
        )

    commentary = request_commentary(commentary_prompt)
    try:
        commentary = validate_commentary(commentary, verified["title"], analysis)
    except SilentFailure as error:
        if str(error) != "commentary has no validated visual-evidence anchor":
            raise
        retry_prompt = (
            commentary_prompt
            + "\nRETRY REQUIREMENT: Your previous reaction lacked a validated anchor. "
            "This time, copy one exact four-or-more-letter word from VISUAL EVIDENCE "
            "into the reaction before making the joke. Do not use a synonym for that word."
        )
        commentary = validate_commentary(
            request_commentary(retry_prompt), verified["title"], analysis
        )

    run_optional_llava_audit(verified["image_path"], image_sha256, analysis, commentary)
    return {
        "status": "ok",
        "title": verified["title"],
        "subreddit": verified["subreddit"],
        "permalink": verified["permalink"],
        "image_path": verified["image_path"],
        "image_sha256": image_sha256,
        "image_analysis": analysis,
        "commentary": commentary,
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        collector_result = json.loads(raw)
        if not isinstance(collector_result, dict):
            raise SilentFailure("collector stdin is not a JSON object")
        print(json.dumps(enrich(collector_result), separators=(",", ":")))
        return 0
    except (SilentFailure, json.JSONDecodeError) as error:
        log(str(error))
        print("[SILENT]")
        return 1
    except Exception as error:
        log(f"unexpected failure: {error}")
        print("[SILENT]")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
