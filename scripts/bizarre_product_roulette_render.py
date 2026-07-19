#!/usr/bin/env python3
"""Render a validated roulette payload; LLMs never format delivery fields.

In no-agent cron mode stdout is delivered verbatim, so successful output is the
final Discord body followed by one Hermes MEDIA directive for the validated image.
"""
import hashlib
import json
import re
import sys
from pathlib import Path


class SilentFailure(Exception):
    """Expected fail-closed condition."""


URL_OR_MENTION_RE = re.compile(r"(?:https?://|www\.|<@|@everyone|@here)", re.IGNORECASE)
REQUIRED_FIELDS = {
    "status",
    "title",
    "subreddit",
    "permalink",
    "image_path",
    "image_sha256",
    "image_analysis",
    "commentary",
}


def fail(message: str) -> SilentFailure:
    print(f"[bizarre-product-render] {message}", file=sys.stderr)
    return SilentFailure(message)


def require_string(payload: dict, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise fail(f"{name} is missing or invalid")
    return value.strip()


def render_for_delivery(payload: dict) -> dict[str, str]:
    """Compose deterministic title/link/media metadata with a constrained reaction."""
    if not isinstance(payload, dict) or set(payload) != REQUIRED_FIELDS:
        raise fail("enriched payload schema is invalid")
    if payload.get("status") != "ok":
        raise fail("enriched payload is not successful")

    title = require_string(payload, "title")
    permalink = require_string(payload, "permalink")
    image_path = require_string(payload, "image_path")
    image_sha256 = require_string(payload, "image_sha256")
    commentary = require_string(payload, "commentary")
    require_string(payload, "subreddit")
    require_string(payload, "image_analysis")

    if any("\n" in value or "\r" in value for value in (title, permalink, image_path, image_sha256)):
        raise fail("deterministic delivery field contains a line break")
    if not permalink.startswith("https://www.reddit.com/"):
        raise fail("permalink is not a canonical Reddit URL")
    if not re.fullmatch(r"[0-9a-f]{64}", image_sha256):
        raise fail("image hash is invalid")
    if URL_OR_MENTION_RE.search(commentary):
        raise fail("commentary contains a URL or mention")

    image = Path(image_path)
    try:
        actual_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
    except OSError as error:
        raise fail(f"image cannot be read: {error}") from error
    if actual_sha256 != image_sha256:
        raise fail("image hash changed before rendering")

    return {
        "content": f"## {title}\n{permalink}\n{commentary}",
        "image_path": image_path,
        "image_sha256": image_sha256,
    }


def render_for_no_agent_delivery(payload: dict) -> str:
    """Return the exact no-agent stdout body consumed by Hermes' delivery router."""
    rendered = render_for_delivery(payload)
    return f"{rendered['content']}\nMEDIA:{rendered['image_path']}"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        print(render_for_no_agent_delivery(payload))
        return 0
    except (SilentFailure, json.JSONDecodeError) as error:
        print(f"[bizarre-product-render] {error}", file=sys.stderr)
        print("[SILENT]")
        return 1
    except Exception as error:
        print(f"[bizarre-product-render] unexpected failure: {error}", file=sys.stderr)
        print("[SILENT]")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
