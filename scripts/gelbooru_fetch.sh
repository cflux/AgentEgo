#!/bin/bash
# Gelbooru Roulette fetcher — pulls a random post for a given tag,
# downloads the full image, prints metadata to stdout.
#
# Usage:
#   gelbooru_fetch.sh <tag>          # downloads to /tmp/gelbooru_find.jpg
#   gelbooru_fetch.sh --sfw <tag>    # filter rating:safe
#
# Output format (final line):
#   url|title|rating|score|tags

set -e

# Re-roll config: try different tags before giving up
MAX_REROLLS=4
SFW_FLAG=""
TAG=""

# Parse args: positional = tag, optional --sfw flag anywhere
for arg in "$@"; do
    if [ "$arg" = "--sfw" ]; then
        SFW_FLAG="--sfw"
    else
        TAG="$arg"
    fi
done

if [ -z "$TAG" ]; then
    echo "Usage: $0 <tag> [--sfw]" >&2
    exit 1
fi

UA="Mozilla/5.0 (Windows NT 10.0; rv:130.0) Gecko/20100101 Firefox/130.0"

LAST_TAG=""
for attempt in $(seq 0 $MAX_REROLLS); do
    if [ "$attempt" -gt 0 ]; then
        # Re-roll with a fresh tag from the deck
        TAG=$(python3 ~/.hermes/scripts/random_gelbooru_tag.py | cut -d'|' -f1)
        if [ "$TAG" = "$LAST_TAG" ]; then
            TAG=$(python3 ~/.hermes/scripts/random_gelbooru_tag.py | cut -d'|' -f1)
        fi
        echo "RE-ROLL #$attempt: trying tag=$TAG" >&2
    fi
    LAST_TAG="$TAG"

    # Sanitize tag for URL
    SAFE_TAG=$(echo "$TAG" | sed 's/ /_/g')
    URL="https://gelbooru.com/index.php?page=post&s=list&tags=${SAFE_TAG}"

    # Fetch the listing page
    LISTING=$(curl -s --max-time 20 -H "User-Agent: $UA" -H "Referer: https://gelbooru.com/" "$URL" || echo "")

    if [ -z "$LISTING" ]; then
        echo "EMPTY:$URL" >&2
        continue
    fi

    # Extract post IDs
    POST_IDS=$(echo "$LISTING" | grep -oP '<a[^>]*id="p\d+"' | grep -oP 'p\d+' | tr -d 'p' | sort -u | head -40)

    if [ -z "$POST_IDS" ]; then
        echo "NO_IDS:$URL" >&2
        continue
    fi

    # Pick a random post ID
    RANDOM_ID=$(echo "$POST_IDS" | shuf -n 1)

    # Fetch the individual post page
    POST_URL="https://gelbooru.com/index.php?page=post&s=view&id=${RANDOM_ID}"
    POST_HTML=$(curl -s --max-time 20 -H "User-Agent: $UA" -H "Referer: https://gelbooru.com/" "$POST_URL")

    if [ -z "$POST_HTML" ]; then
        echo "NO_POST:$RANDOM_ID" >&2
        continue
    fi

    # Extract rating
    RATING=$(echo "$POST_HTML" | grep -oP 'rating[^>]*>\K(explicit|safe|questionable)' | head -1)
    if [ -z "$RATING" ]; then
        RATING=$(echo "$POST_HTML" | grep -oP 'alt="\K(explicit|safe|questionable)(?=")' | head -1)
    fi
    RATING="${RATING:-questionable}"

    # SFW filter - re-roll if explicit and user wants safe
    if [ "$SFW_FLAG" = "--sfw" ] && [ "$RATING" = "explicit" ]; then
        echo "EXPLICIT_FILTERED:$RANDOM_ID" >&2
        continue
    fi

    # Extract image URL — full-res, fallback chain
    IMG_URL=$(echo "$POST_HTML" | grep -oP 'id="image"[^>]*src="\Khttps?://img[0-9]*\.gelbooru\.com//images/[^"]+\.(?:jpg|jpeg|png|webp)' | head -1)
    if [ -z "$IMG_URL" ]; then
        IMG_URL=$(echo "$POST_HTML" | grep -oP 'id="image"[^>]*href="\Khttps?://img[0-9]*\.gelbooru\.com//images/[^"]+\.(?:jpg|jpeg|png|webp)' | head -1)
    fi
    if [ -z "$IMG_URL" ]; then
        IMG_URL=$(echo "$POST_HTML" | grep -oP 'Original\s*image[^<]*</[^>]+>[^<]*<a[^>]*href="\Khttps?://img[0-9]*\.gelbooru\.com//images/[^"]+\.(?:jpg|jpeg|png|webp)' | head -1)
    fi
    if [ -z "$IMG_URL" ]; then
        IMG_URL=$(echo "$POST_HTML" | grep -oP 'https?://img[0-9]*\.gelbooru\.com//images/[^"]+\.(?:jpg|jpeg|png|webp)' | head -1)
    fi

    if [ -z "$IMG_URL" ]; then
        echo "NO_IMG:$RANDOM_ID" >&2
        continue
    fi

    # Extract title (fall back to the tag itself)
    TITLE="$TAG"

    # Extract score
    SCORE=$(echo "$POST_HTML" | grep -oP 'Score:\s*</span>\s*<[^>]+>\s*\K-?\d+' | head -1)
    SCORE="${SCORE:-0}"

    # Extract tags (top 20)
    TAGS=$(echo "$POST_HTML" | grep -oP 'data-tag-name="\K[^"]+' | head -20 | tr '\n' ',' | sed 's/,$//')

    # Download the image
    curl -s --max-time 30 -L -H "User-Agent: $UA" -H "Referer: https://gelbooru.com/" "$IMG_URL" -o /tmp/gelbooru_find.jpg

    if [ ! -s /tmp/gelbooru_find.jpg ] || [ $(stat -c%s /tmp/gelbooru_find.jpg 2>/dev/null || echo 0) -lt 5000 ]; then
        echo "DOWNLOAD_FAILED:$IMG_URL" >&2
        rm -f /tmp/gelbooru_find.jpg
        continue
    fi

    # ── Size & type guard (keep Discord-compatible output, reject the rest) ──
    FILE_SIZE=$(stat -c%s /tmp/gelbooru_find.jpg 2>/dev/null || echo 0)
    FILE_TYPE=$(file -b /tmp/gelbooru_find.jpg 2>/dev/null || echo "unknown")
    if [ "$FILE_SIZE" -gt 8388608 ] || echo "$FILE_TYPE" | grep -qiE 'MP4|WebM|video|animation|movie'; then
        echo "REJECTED: $FILE_TYPE, $FILE_SIZE bytes (tag=$TAG, id=$RANDOM_ID)" >&2
        rm -f /tmp/gelbooru_find.jpg
        continue
    fi

    # Success — output and exit the loop
    echo "${IMG_URL}|${TITLE}|${RATING}|${SCORE}|${TAGS}"
    exit 0
done

# All attempts exhausted
echo "GIVE_UP:$MAX_REROLLS attempts, no usable post" >&2
exit 1
