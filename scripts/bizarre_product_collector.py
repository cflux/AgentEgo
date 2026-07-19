#!/usr/bin/env python3
"""Collect one validated bizarre-product image from Reddit Atom/RSS.

Stdout is reserved for one strict JSON success object. Diagnostics go to stderr;
all acquisition failures exit non-zero so a scheduled formatter can emit [SILENT].
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from reddit_request_pacing import RedditRequestGate, reddit_open

FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) "
    "Gecko/20100101 Firefox/130.0"
)
COOKIE = "over18=1"
DOWNLOAD_TIMEOUT = 30
REDDIT_GATE = RedditRequestGate()
SUBREDDITS = (
    "AmazonWTF",
    "WTF_Amazon",
    "CrackheadCraigslist",
    "ATBGE",
    "DiWHY",
    "CrappyDesign",
    "ExpectationVsReality",
)
ATOM = {"atom": "http://www.w3.org/2005/Atom"}
DIRECT_IMAGE_RE = re.compile(r'https://i\.redd\.it/[^\s"<>]+?\.(?:jpe?g|png)(?:\?[^\s"<>]*)?', re.I)
PERMALINK_RE = re.compile(
    r'https://www\.reddit\.com/r/[^/\s"<>]+/comments/[^/\s"<>]+(?:/[^\s"<>]*)?',
    re.I,
)
REDDIT_PERMALINK_RE = re.compile(
    r"^https://www\.reddit\.com/r/[^/]+/comments/[^/]+(?:/.*)?$", re.I
)


def log(message: str) -> None:
    print(f"[bizarre-product-collector] {message}", file=sys.stderr)


def feed_url(subreddit: str) -> str:
    """Return the RSS-only source URL for a curated subreddit."""
    return f"https://www.reddit.com/r/{quote(subreddit, safe='')}/top.rss?t=week"


def request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={"User-Agent": FIREFOX_UA, "Cookie": COOKIE},
    )


def parse_feed(raw: str, subreddit: str) -> list[dict[str, str]]:
    """Extract direct image candidates and canonical Reddit post links from Atom."""
    root = ET.fromstring(raw)
    candidates: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ATOM):
        title = (entry.findtext("atom:title", default="", namespaces=ATOM) or "").strip()
        content = entry.findtext("atom:content", default="", namespaces=ATOM) or ""
        image = DIRECT_IMAGE_RE.search(content)
        permalink = PERMALINK_RE.search(content)
        if not title or image is None or permalink is None:
            continue
        candidates.append(
            {
                "title": title,
                "subreddit": subreddit,
                "image_url": image.group(0),
                "permalink": permalink.group(0),
            }
        )
    return candidates


def fetch_feed(subreddit: str) -> str:
    with reddit_open(
        request(feed_url(subreddit)), timeout=DOWNLOAD_TIMEOUT, gate=REDDIT_GATE
    ) as response:
        return response.read().decode("utf-8")


def has_supported_image_signature(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")


def download(image_url: str, directory: str) -> str:
    """Download valid direct image content into this run's unique directory."""
    with reddit_open(request(image_url), timeout=DOWNLOAD_TIMEOUT, gate=REDDIT_GATE) as response:
        data = response.read()
    if not has_supported_image_signature(data):
        raise RuntimeError("download was not a JPEG or PNG")
    extension = ".png" if data.startswith(b"\x89PNG") else ".jpg"
    destination = Path(directory) / f"product{extension}"
    destination.write_bytes(data)
    return str(destination)


def validate_result(result: dict[str, str]) -> dict[str, str]:
    required = {"status", "title", "subreddit", "permalink", "image_path"}
    if set(result) != required:
        raise ValueError("result fields do not match the strict contract")
    if result["status"] != "ok":
        raise ValueError("result status is not ok")
    if not all(isinstance(result[field], str) and result[field].strip() for field in required):
        raise ValueError("result contains an empty required field")
    if not REDDIT_PERMALINK_RE.fullmatch(result["permalink"]):
        raise ValueError("result permalink is not a Reddit comments URL")
    media = Path(result["image_path"])
    if not media.is_file() or media.stat().st_size == 0:
        raise ValueError("result image is missing or empty")
    if not has_supported_image_signature(media.read_bytes()[:8]):
        raise ValueError("result image has an unsupported signature")
    return result


def collect_one(
    subreddits: list[str] | tuple[str, ...],
    *,
    fetch_feed: Callable[[str], str] = fetch_feed,
    download: Callable[[str, str], str] = download,
    chooser: Callable[[list[dict[str, str]]], dict[str, str]] = random.choice,
) -> dict[str, str]:
    """Fetch one chosen subreddit and return exactly one validated result.

    One feed per scheduled run deliberately bounds anonymous RSS traffic. A failure
    cannot fall through to a prior run's output because the media directory is unique.
    """
    if not subreddits:
        raise RuntimeError("no subreddits configured")
    subreddit = random.choice(list(subreddits))
    candidates = parse_feed(fetch_feed(subreddit), subreddit)
    if not candidates:
        raise RuntimeError(f"no direct-image candidates in r/{subreddit}")
    candidate = chooser(candidates)
    run_directory = tempfile.mkdtemp(prefix="bizarre-product-", dir="/tmp")
    image_path = download(candidate["image_url"], run_directory)
    return validate_result(
        {
            "status": "ok",
            "title": candidate["title"],
            "subreddit": candidate["subreddit"],
            "permalink": candidate["permalink"],
            "image_path": image_path,
        }
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subreddit",
        choices=SUBREDDITS,
        help="Use one curated subreddit instead of the normal random selection.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    subreddits = [args.subreddit] if args.subreddit else list(SUBREDDITS)
    try:
        result = collect_one(subreddits)
    except Exception as error:
        log(f"collection failed: {error}")
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
