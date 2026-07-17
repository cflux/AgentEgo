#!/usr/bin/env python3
"""
Reddit Video Roulette — Collector
==================================
Picks a random subreddit, finds a v.redd.it post, downloads it,
extracts 2 frames, runs VLM analysis on both via Salt vLLM.

Outputs JSON to stdout. Cleanup of video happens here; frame cleanup
is handled by the cron prompt after the LLM posts them.

Usage:
    python3 reddit_video_collector.py [--nsfw]
    python3 reddit_video_collector.py --subs funny videos aww
    python3 reddit_video_collector.py --nsfw --subs gonewildcurvy
"""

import base64
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

# ── CONFIG ───────────────────────────────────────────────────
OUTPUT_DIR = "/tmp/reddit_video"
VLM_ENDPOINT = "http://salt-gx10.local:8000/v1/chat/completions"
VLM_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct-AWQ"
VLM_TIMEOUT = 180
DOWNLOAD_TIMEOUT = 30

# Video-heavy SFW subreddits — subs where v.redd.it posts are common
VIDEO_SUBS = [
    "funny", "gifs", "interestingasfuck", "nextfuckinglevel",
    "oddlysatisfying", "BeAmazed", "woahdude", "NatureIsFuckingLit",
    "aww", "AnimalsBeingDerps", "WhatsWrongWithYourDog",
    "Whatcouldgowrong", "AbruptChaos", "Unexpected",
    "blackmagicfuckery", "youseeingthisshit", "instant_regret",
    "contagiouslaughter", "BetterEveryLoop", "nonononoyes",
    "WatchPeopleDieInside", "StartledCats", "Zoomies",
    "holdmyredbull", "therewasanattempt", "Wellthatsucks",
    "PublicFreakout", "KillTheCameraMan", "PraiseTheCameraMan",
]

# NSFW subs — empty by default. Add video-heavy NSFW subs here to enable.
# The --nsfw flag only activates these if the list is non-empty (safety guard).
NSFW_SUBS: list[str] = ["HENTAI_GIF","Curvy_Women_Gone_Wild", "gifsgonewild", "NSFW_GIF", "AdultNSFW_Gifs", "HomemadeNsfw", "DirtyGaming",]

# ── BECCA COMMENTARY ──────────────────────────────────────────
# Randomly selected one-liners injected into the post. Variables:
#   {sub}   — subreddit name
#   {title} — video title (first 40 chars)
COMMENTARY = [
    "r/{sub} never disappoints. Or does. One of those.",
    "Another day, another r/{sub} fever dream.",
    "The internet is a beautiful garbage fire and r/{sub} is the kindling.",
    "If you can't be the smartest person in the room, at least be the most entertaining. r/{sub} gets it.",
    "Preem find from the depths of Reddit's finest dumpster.",
    "This is why we can't have nice things. And I'm here for it.",
    "Your daily reminder that someone, somewhere, recorded this instead of calling for help.",
    "r/{sub} serving up exactly what you didn't know you needed.",
    "No context, no mercy. Just pure r/{sub} energy.",
    "If this doesn't make you feel something, check your pulse.",
    "I have questions. r/{sub} has no answers.",
    "The camera work is ✨ art ✨. The subject matter is... something else.",
    "Zero notes. Perfect. No one tell the corpos about r/{sub}.",
    "This has 'I should be doing something productive' energy and I respect it.",
    "r/{sub}: where common sense goes to die and I'm buying front-row seats.",
]

def pick_commentary(sub: str, title: str) -> str:
    """Pick a random commentary line and interpolate variables."""
    line = random.choice(COMMENTARY)
    return line.format(sub=sub, title=title[:40])


# ── HELPERS ──────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[video-collector] {msg}", file=sys.stderr)


def fetch_video_posts(subreddit: str) -> list[dict]:
    """Fetch top RSS from today and extract v.redd.it posts."""
    url = f"https://www.reddit.com/r/{subreddit}/top.rss?t=day"
    log(f"Fetching RSS: r/{subreddit}")

    req = urllib.request.Request(url, headers={
        "User-Agent": "Hermes/1.0 (by u/AlternateFlux; local script; contact via DM)"
    })

    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")

    root = ET.fromstring(raw)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    posts = []
    for entry in root.findall("atom:entry", ns):
        content_el = entry.find("atom:content", ns)
        if content_el is None or not content_el.text:
            continue

        # Find v.redd.it URLs in the content HTML
        vreddit_urls = re.findall(r'https?://v\.redd\.it/[^\s"<>]+', content_el.text)
        if not vreddit_urls:
            continue

        link_el = entry.find("atom:link", ns)
        permalink = link_el.get("href") if link_el is not None else ""

        title_el = entry.find("atom:title", ns)
        title = title_el.text if title_el is not None else "Untitled"

        posts.append({
            "vreddit_url": vreddit_urls[0],
            "permalink": permalink,
            "title": title,
            "subreddit": subreddit,
        })

    return posts


def download_video(vreddit_url: str) -> str:
    """Download Reddit video with yt-dlp. Returns path to mp4."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_tmpl = os.path.join(OUTPUT_DIR, "video.%(ext)s")

    cmd = [
        "yt-dlp", "-q", "--no-warnings",
        "-f", "bestvideo+bestaudio/best",
        "-o", output_tmpl,
        "--merge-output-format", "mp4",
        "--no-playlist",
        vreddit_url,
    ]

    subprocess.run(cmd, timeout=120, check=True, capture_output=True)

    # Find whatever file yt-dlp created
    for fname in os.listdir(OUTPUT_DIR):
        if fname.startswith("video.") and not fname.endswith(".part"):
            return os.path.join(OUTPUT_DIR, fname)

    raise FileNotFoundError(f"No output file found in {OUTPUT_DIR}")


def get_duration(video_path: str) -> float:
    """Get video duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, timeout=10,
    )
    return float(result.stdout.strip())


def extract_frame(video_path: str, timestamp: float, output_path: str) -> None:
    """Extract a single frame at the given timestamp (seconds)."""
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(timestamp),
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        output_path,
    ], check=True, timeout=30)


def get_vlm_blurb(image_path: str, prompt: str) -> str:
    """Ask Salt's vLLM to describe a video frame (OpenAI-compatible endpoint)."""
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type:
        mime_type = "image/jpeg"

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    data_url = f"data:{mime_type};base64,{b64}"

    body = json.dumps({
        "model": VLM_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": 50,
        "temperature": 0.5,
    }).encode()

    req = urllib.request.Request(
        VLM_ENDPOINT, data=body,
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=VLM_TIMEOUT) as resp:
        d = json.loads(resp.read())
        return d["choices"][0]["message"]["content"].strip().strip('"').strip("'")


# ── MAIN ─────────────────────────────────────────────────────
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Reddit Video Roulette — Collector")
    parser.add_argument("--nsfw", action="store_true",
                        help="Include NSFW subreddits (only if NSFW_SUBS list is non-empty)")
    parser.add_argument("--subs", nargs="*",
                        help="Override subreddit list (space-separated)")
    args = parser.parse_args()

    log("Reddit Video Roulette — starting")

    # Build candidate pool
    if args.subs:
        subs = args.subs
        log(f"Using --subs override: {len(subs)} subreddits")
    else:
        subs = list(VIDEO_SUBS)  # copy
        if args.nsfw:
            if NSFW_SUBS:
                subs += NSFW_SUBS
                log(f"NSFW mode enabled (+{len(NSFW_SUBS)} subs, {len(subs)} total)")
            else:
                log("--nsfw passed but NSFW_SUBS list is empty — ignoring (safety guard)")

    # Step 1: Pick random video subreddit
    sub = random.choice(subs)
    log(f"Selected: r/{sub}")

    # Step 2: Fetch video posts
    posts = fetch_video_posts(sub)

    # Fallback: try a guaranteed video-heavy sub
    if not posts:
        log(f"No v.redd.it posts in r/{sub}, trying backup sub...")
        for fallback in ["funny", "videos", "gifs", "nextfuckinglevel"]:
            posts = fetch_video_posts(fallback)
            if posts:
                sub = fallback
                break

    if not posts:
        log("No video posts found anywhere. Exiting.")
        sys.exit(1)

    log(f"Found {len(posts)} video posts in r/{sub}")

    # Step 3: Pick a post and download
    post = random.choice(posts)
    log(f"Downloading: \"{post['title'][:60]}\"")

    video_path = None
    for attempt in range(min(3, len(posts))):
        candidate = posts[attempt]
        try:
            video_path = download_video(candidate["vreddit_url"])
            post = candidate
            break
        except Exception as e:
            log(f"Download failed for candidate {attempt}: {e}")
            # Clean up any partial download
            if os.path.exists(OUTPUT_DIR):
                for f in os.listdir(OUTPUT_DIR):
                    if f.startswith("video."):
                        os.remove(os.path.join(OUTPUT_DIR, f))
            continue

    if video_path is None:
        log("All downloads failed. Exiting.")
        sys.exit(1)

    # Step 4: Get duration
    try:
        duration = get_duration(video_path)
        log(f"Downloaded: {os.path.basename(video_path)} ({duration:.1f}s)")
    except Exception as e:
        log(f"ffprobe failed: {e}, defaulting to 30s")
        duration = 30.0

    # Skip very short videos (< 3s — probably a GIF masquerading)
    if duration < 3.0:
        log(f"Video too short ({duration:.1f}s), skipping")
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
        sys.exit(1)

    # Step 5: Extract 2 frames — one early, one late
    frame1_ts = max(1.0, duration * 0.20)   # 20% in, min 1s
    frame2_ts = min(duration - 1.0, duration * 0.70)  # 70% in, min 1s from end

    frame1_path = os.path.join(OUTPUT_DIR, "frame1.jpg")
    frame2_path = os.path.join(OUTPUT_DIR, "frame2.jpg")

    try:
        extract_frame(video_path, frame1_ts, frame1_path)
        log(f"Frame 1 extracted at {frame1_ts:.1f}s ({os.path.getsize(frame1_path)} bytes)")
        extract_frame(video_path, frame2_ts, frame2_path)
        log(f"Frame 2 extracted at {frame2_ts:.1f}s ({os.path.getsize(frame2_path)} bytes)")
    except Exception as e:
        log(f"Frame extraction failed: {e}")
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
        sys.exit(1)

    # Step 6: Delete the video — keep frames for the LLM
    os.remove(video_path)
    log(f"Deleted video, frames remain in {OUTPUT_DIR}")

    # Step 7: VLM analysis on both frames
    frame_prompt = "Describe this video frame in EXACTLY one sentence. What's happening? Be specific."

    try:
        frame1_blurb = get_vlm_blurb(frame1_path, frame_prompt)
        log(f"Frame 1 VLM: {frame1_blurb}")
    except Exception as e:
        log(f"VLM frame 1 failed: {e}")
        frame1_blurb = "A scene from the video."

    try:
        frame2_blurb = get_vlm_blurb(frame2_path, frame_prompt)
        log(f"Frame 2 VLM: {frame2_blurb}")
    except Exception as e:
        log(f"VLM frame 2 failed: {e}")
        frame2_blurb = "Another scene from the video."

    # Step 8: Build the Discord post
    commentary = pick_commentary(post["subreddit"], post["title"])

    post = (
        f"🎬 **Reddit Video Roulette** — r/{post['subreddit']}\n\n"
        f"MEDIA:{frame1_path}\n\n"
        f"> {frame1_blurb} *(~{round(frame1_ts, 1)}s)*\n\n"
        f"MEDIA:{frame2_path}\n\n"
        f"> {frame2_blurb} *(~{round(frame2_ts, 1)}s)*\n\n"
        f"{commentary}\n\n"
        f"[Source]({post['permalink']})"
    )

    # Step 9: Clean up frames — delivery is instant, frames are served
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    log("Frames cleaned. Post delivered below.")

    print(post)

    # Step 10: Log structured data to stderr for debugging
    log(json.dumps({
        "subreddit": post["subreddit"],
        "title": post["title"],
        "duration": round(duration, 1),
        "commentary": commentary,
    }))


if __name__ == "__main__":
    main()
