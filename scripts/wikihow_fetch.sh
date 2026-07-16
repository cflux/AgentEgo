#!/bin/bash
# WikiHow Roulette — fetches a random article, extracts title + featured image.
# Output: TITLE|IMAGE_URL|ARTICLE_URL (pipe-delimited)

RANDOM_URL=$(curl -sI -o /dev/null -w '%{redirect_url}' "https://www.wikihow.com/Special:Randomizer")
if [ -z "$RANDOM_URL" ]; then
    exit 1
fi

HTML=$(curl -s -L -H "User-Agent: Mozilla/5.0" "$RANDOM_URL")
TITLE=$(echo "$HTML" | grep -oP '<meta property="og:title" content="\K[^"]+' | head -1)
IMAGE=$(echo "$HTML" | grep -oP 'property="og:image" content="\K[^"]+' | head -1)

echo "${TITLE}|${IMAGE}|${RANDOM_URL}"
