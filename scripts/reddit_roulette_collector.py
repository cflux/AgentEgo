#!/usr/bin/env python3
"""Reddit Image Roulette collector using the central Reddit client."""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import random
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from reddit_client import RedditClient, RedditClientError, RedditPost, RedditRateLimited

OUTPUT_DIR = "/tmp/reddit_roulette"
VLM_ENDPOINT = "http://salt-gx10.local:8000/v1/chat/completions"
VLM_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct-AWQ"
VLM_TIMEOUT = 240
DOWNLOAD_TIMEOUT = 30  # retained for compatibility with callers/configuration

SFW_SUBS = ["aww", "funny", "pics", "cats", "Eyebleach", "mademesmile",
            "natureisfuckinglit", "interestingasfuck", "oddlysatisfying",
            "damnthatsinteresting", "BeAmazed"]
NSFW_SUBS = ["rule34", "NSFW", "gonewild", "RealGirls", "Amateur",
             "ass", "LegalTeens", "DirtyGaming", "hentai", "Curvy_Women_Gone_Wild",
             "gonewildATwork", "AsiansGoneWild"]

CLIENT = RedditClient()


def log(msg: str) -> None:
    """Print diagnostics to stderr so stdout stays strict JSON."""
    print(f"[collector] {msg}", file=sys.stderr)


def _post_dict(post: RedditPost) -> dict[str, str]:
    return {
        "title": post.title,
        "image_url": post.media_url,
        "permalink": post.permalink,
        "subreddit": post.subreddit,
    }


def fetch_rss(subreddit: str) -> list[dict[str, str]]:
    """Compatibility wrapper: central client fetch plus image-only dicts."""
    log(f"Fetching RSS: r/{subreddit}")
    posts = CLIENT.fetch_rss(subreddit)
    image_posts = [_post_dict(post) for post in posts if post.media_kind == "image"]
    log(f"  → {len(image_posts)} image posts found in r/{subreddit}")
    return image_posts


def download_image(url: str, filename: str) -> str:
    """Compatibility wrapper around centralized validated atomic download."""
    path = CLIENT.download_media(url, Path(OUTPUT_DIR), stem=Path(filename).stem)
    return str(path)


def collect_image_posts(subreddits: list[str]) -> list[dict[str, str]]:
    """Collect feeds until exhausted or Reddit terminally rate-limits this run."""
    all_posts: list[dict[str, str]] = []
    for sub in subreddits:
        try:
            all_posts.extend(fetch_rss(sub))
        except RedditRateLimited:
            log(f"Reddit rate limited r/{sub}; stopping feed scan: error_type=RedditRateLimited")
            break
        except Exception as error:
            log(f"Failed r/{sub}: error_type={type(error).__name__}")
    return all_posts


def get_vlm_blurb(image_path: str) -> str:
    """Ask Salt's vLLM to describe an image in exactly 10 words."""
    mime_type, _ = mimetypes.guess_type(image_path)
    mime_type = mime_type or "image/jpeg"
    with open(image_path, "rb") as handle:
        b64 = base64.b64encode(handle.read()).decode()
    body = json.dumps({
        "model": VLM_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            {"type": "text", "text": "Describe this image in EXACTLY 10 words. No commentary, no filler, no 'The image shows'. Just the description."},
        ]}],
        "max_tokens": 30,
        "temperature": 0.7,
    }).encode()
    request = urllib.request.Request(
        VLM_ENDPOINT, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=VLM_TIMEOUT) as response:
        data = json.loads(response.read())
        return data["choices"][0]["message"]["content"].strip().strip('"').strip("'")


def _is_timeout_error(error: BaseException) -> bool:
    """Return whether an exception represents the bounded Salt request timing out."""
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    return isinstance(error, urllib.error.URLError) and isinstance(
        error.reason, (TimeoutError, socket.timeout)
    )


def main(argv: list[str] | None = None) -> int:
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nsfw", action="store_true")
    parser.add_argument("--subs", nargs="*")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.subs:
        subs = args.subs
    else:
        subs = list(SFW_SUBS)
        if args.nsfw:
            subs.extend(NSFW_SUBS)
    log(f"Target subreddits: {subs}")

    try:
        with CLIENT.single_flight():
            # Deliberately retain the unique directory after process exit: the cron
            # formatter resolves MEDIA paths after the collector returns.
            OUTPUT_DIR = tempfile.mkdtemp(prefix="reddit-roulette-", dir="/tmp")
            all_posts = collect_image_posts(subs)
            if not all_posts:
                log("No image posts found across any subreddit. Exiting.")
                return 1

            candidates = random.sample(all_posts, min(3, len(all_posts)))
            log(f"Selected {len(candidates)} candidates from {len(all_posts)} total")
            results = []
            for index, post in enumerate(candidates):
                try:
                    local_path = download_image(post["image_url"], f"candidate_{index}")
                    log(f"Downloaded: {local_path} ({os.path.getsize(local_path)} bytes)")
                    vlm_started = time.monotonic()
                    try:
                        blurb = get_vlm_blurb(local_path)
                    except Exception as error:
                        elapsed = time.monotonic() - vlm_started
                        status = "timeout" if _is_timeout_error(error) else "error"
                        log(
                            "VLM_TIMING "
                            f"candidate={index} status={status} elapsed_s={elapsed:.3f} "
                            f"error_type={type(error).__name__} "
                            f"timeout_s={VLM_TIMEOUT} "
                            f"timeout_margin_s={VLM_TIMEOUT - elapsed:.3f}"
                        )
                        continue
                    elapsed = time.monotonic() - vlm_started
                    log(f"VLM_TIMING candidate={index} status=ok elapsed_s={elapsed:.3f}")
                    results.append({
                        "path": local_path,
                        "title": post["title"],
                        "subreddit": post["subreddit"],
                        "permalink": post["permalink"],
                        "vlm_blurb": blurb,
                    })
                except Exception as error:
                    log(f"Failed candidate {index}: error_type={type(error).__name__}")

            if not results:
                log("All candidates failed. Exiting.")
                return 1
            print(json.dumps({"candidates": results}, indent=2))
            return 0
    except RedditClientError as error:
        log(f"Reddit client failed closed: error_type={type(error).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
