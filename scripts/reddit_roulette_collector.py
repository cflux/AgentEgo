#!/usr/bin/env python3
"""
Reddit Image Roulette — Collector (RSS Edition)
================================================
Scrapes Reddit RSS feeds → downloads image posts → runs VLM blurbs.
No OAuth. No API limits. No JS shells.

Outputs JSON with image paths, titles, and VLM blurbs to stdout.
The LLM just reads it and writes a Becca-style caption.

Usage:
    python3 reddit_roulette_collector.py [--nsfw]
    python3 reddit_roulette_collector.py --subs aww funny pics
    
Flags:
    --nsfw     Include NSFW subreddits (r/nsfw, r/gonewild, etc.)
    --subs     Override subreddit list (space-separated)
"""
import base64
import json
import os
import random
import sys
import urllib.request
import xml.etree.ElementTree as ET

from reddit_request_pacing import RedditRateLimited, RedditRequestGate, reddit_open

# ── CONFIG ───────────────────────────────────────────────────
OUTPUT_DIR = "/tmp/reddit_roulette"
VLM_ENDPOINT = "http://salt-gx10.local:8000/v1/chat/completions"
VLM_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct-AWQ"
VLM_TIMEOUT = 180
DOWNLOAD_TIMEOUT = 30
REDDIT_GATE = RedditRequestGate()

SFW_SUBS = ["aww", "funny", "pics", "cats", "Eyebleach", "mademesmile",
            "natureisfuckinglit", "interestingasfuck", "oddlysatisfying",
            "damnthatsinteresting", "BeAmazed"]
NSFW_SUBS = ["rule34","NSFW", "gonewild", "RealGirls", "Amateur",
             "ass", "LegalTeens","DirtyGaming","hentai","Curvy_Women_Gone_Wild","gonewildATwork","AsiansGoneWild"]

# ── HELPERS ──────────────────────────────────────────────────
def log(msg):
    """Print to stderr so stdout stays JSON-clean."""
    print(f"[collector] {msg}", file=sys.stderr)

def fetch_rss(subreddit: str) -> list[dict]:
    """Fetch RSS for a subreddit and extract image posts."""
    url = f"https://www.reddit.com/r/{subreddit}/top.rss?t=day"
    log(f"Fetching RSS: {url}")
    
    req = urllib.request.Request(url, headers={
        "User-Agent": "Hermes/1.0 (by u/AlternateFlux; local script; contact via DM)"
    })
    
    with reddit_open(req, timeout=DOWNLOAD_TIMEOUT, gate=REDDIT_GATE) as resp:
        raw = resp.read().decode("utf-8")
    
    root = ET.fromstring(raw)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    
    posts = []
    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        content_el = entry.find("atom:content", ns)
        
        if title_el is None or content_el is None:
            continue
        
        title = title_el.text or ""
        content_html = content_el.text or ""
        
        # Extract i.redd.it direct image URL from the content HTML
        import re
        img_match = re.search(r'href="(https://i\.redd\.it/[^"]+\.(jpg|jpeg|png))"', content_html)
        if not img_match:
            continue
        
        image_url = img_match.group(1)
        
        # Also find the reddit post permalink
        permalink_match = re.search(r'href="(https://www\.reddit\.com/r/[^"]+/comments/[^"]+)"', content_html)
        permalink = permalink_match.group(1) if permalink_match else ""
        
        posts.append({
            "title": title,
            "image_url": image_url,
            "permalink": permalink,
            "subreddit": subreddit
        })
    
    log(f"  → {len(posts)} image posts found in r/{subreddit}")
    return posts


def download_image(url: str, filename: str) -> str:
    """Download an image, return local path."""
    path = os.path.join(OUTPUT_DIR, filename)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Hermes/1.0 (by u/AlternateFlux; local script)"
    })
    with reddit_open(req, timeout=DOWNLOAD_TIMEOUT, gate=REDDIT_GATE) as resp:
        data = resp.read()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def collect_image_posts(subreddits: list[str]) -> list[dict]:
    """Collect feeds until exhausted or Reddit rate-limits this run."""
    all_posts = []
    for sub in subreddits:
        try:
            all_posts.extend(fetch_rss(sub))
        except RedditRateLimited as error:
            log(f"Reddit rate limited r/{sub}; stopping feed scan: {error}")
            break
        except Exception as error:
            log(f"Failed r/{sub}: {error}")
    return all_posts


def get_vlm_blurb(image_path: str) -> str:
    """Ask Salt's vLLM to describe an image in ~10 words (OpenAI-compatible endpoint)."""
    import mimetypes
    
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
                {"type": "text", "text": "Describe this image in EXACTLY 10 words. No commentary, no filler, no 'The image shows'. Just the description."}
            ]
        }],
        "max_tokens": 30,
        "temperature": 0.7
    }).encode()
    
    req = urllib.request.Request(VLM_ENDPOINT, data=body,
                                  headers={"Content-Type": "application/json"})
    
    with urllib.request.urlopen(req, timeout=VLM_TIMEOUT) as resp:
        d = json.loads(resp.read())
        return d["choices"][0]["message"]["content"].strip().strip('"').strip("'")
    

# ── MAIN ─────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    include_nsfw = "--nsfw" in args
    
    # Check for --subs override
    custom_subs = []
    if "--subs" in args:
        idx = args.index("--subs")
        custom_subs = args[idx+1:] if idx+1 < len(args) else []
    
    if custom_subs:
        subs = custom_subs
    else:
        subs = list(SFW_SUBS)
        if include_nsfw:
            subs.extend(NSFW_SUBS)
    
    log(f"Target subreddits: {subs}")
    
    # Phase 1: Collect candidates from RSS
    all_posts = collect_image_posts(subs)
    
    if not all_posts:
        log("No image posts found across any subreddit. Exiting.")
        sys.exit(1)
    
    # Pick 3 random candidates
    candidates = random.sample(all_posts, min(3, len(all_posts)))
    log(f"Selected {len(candidates)} candidates from {len(all_posts)} total")
    
    # Phase 2: Download images + run VLM
    results = []
    for i, post in enumerate(candidates):
        try:
            ext = post["image_url"].rsplit(".", 1)[-1].split("?")[0]
            filename = f"candidate_{i}.{ext}"
            local_path = download_image(post["image_url"], filename)
            
            log(f"Downloaded: {local_path} ({os.path.getsize(local_path)} bytes)")
            
            # Query VLM
            blurb = get_vlm_blurb(local_path)
            log(f"VLM blurb: {blurb}")
            
            results.append({
                "path": local_path,
                "title": post["title"],
                "subreddit": post["subreddit"],
                "permalink": post["permalink"],
                "vlm_blurb": blurb
            })
        except Exception as e:
            log(f"Failed candidate {i} ({post['image_url'].rsplit('/',1)[-1]}): {e}")
            continue
    
    if not results:
        log("All candidates failed. Exiting.")
        sys.exit(1)
    
    # Output JSON to stdout
    print(json.dumps({"candidates": results}, indent=2))
    # Files stay in /tmp/reddit_roulette/ — Hermes handles MEDIA delivery,
    # and /tmp gets purged on reboot. No early cleanup. 


if __name__ == "__main__":
    main()
