#!/usr/bin/env python3
"""Collect verified Portland weather and fresh Hacker News items for the morning briefing."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
from typing import Any, Callable

WEATHER_URL = "https://wttr.in/Portland?format=j1"
HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
MAX_NEWS_AGE = dt.timedelta(hours=36)
RELEVANCE_TERMS = (
    "ai", "artificial intelligence", "software", "security", "cyber", "hack", "vulnerability",
    "open source", "linux", "python", "database", "model", "gpu", "chip", "cloud", "web", "data",
    "robot", "programming", "developer", "internet", "technology", "tech",
)


def _get_json(url: str, opener: Callable[..., Any] = urllib.request.urlopen) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Hermes-Night-City-Briefing/1.0"},
    )
    with opener(request, timeout=15) as response:
        return json.load(response)


def _num(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return float(value) if "." in str(value) else int(value)
    except (TypeError, ValueError):
        return value


def parse_weather(payload: dict[str, Any]) -> dict[str, Any]:
    current = payload["current_condition"][0]
    today = payload["weather"][0]
    hourly = today.get("hourly", [])
    descriptions = [
        str(hour.get("weatherDesc", [{}])[0].get("value", "")).strip()
        for hour in hourly
        if hour.get("weatherDesc")
    ]
    descriptions = list(dict.fromkeys(item for item in descriptions if item))
    precip_chances = [
        _num(hour.get("chanceofrain")) for hour in hourly if hour.get("chanceofrain") not in (None, "")
    ]
    rain_mm = [
        _num(hour.get("precipMM")) for hour in hourly if hour.get("precipMM") not in (None, "")
    ]
    return {
        "location": "Portland",
        "current": {
            "temp_f": _num(current.get("temp_F")),
            "feels_like_f": _num(current.get("FeelsLikeF")),
            "description": current.get("weatherDesc", [{}])[0].get("value", "Unknown"),
            "humidity_percent": _num(current.get("humidity")),
        },
        "today": {
            "date": today.get("date"),
            "high_f": _num(today.get("maxtempF")),
            "low_f": _num(today.get("mintempF")),
            "total_snow_cm": _num(today.get("totalSnow_cm")),
            "max_precip_chance_percent": max(precip_chances) if precip_chances else None,
            "max_precip_mm": max(rain_mm) if rain_mm else None,
            "descriptions": descriptions,
            "astronomy": today.get("astronomy", []),
        },
    }


def _published_at(item: dict[str, Any]) -> dt.datetime | None:
    timestamp = item.get("time")
    if not isinstance(timestamp, (int, float)):
        return None
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)


def collect_news(
    now: dt.datetime | None = None,
    get_json: Callable[[str], Any] = _get_json,
    limit: int = 20,
) -> list[dict[str, Any]]:
    now = now or dt.datetime.now(dt.timezone.utc)
    ids = get_json(HN_TOP_URL)
    accepted: list[dict[str, Any]] = []
    for item_id in list(ids)[:limit]:
        item = get_json(HN_ITEM_URL.format(item_id=urllib.parse.quote(str(item_id))))
        if not isinstance(item, dict) or item.get("type") != "story":
            continue
        published = _published_at(item)
        if published is None:
            continue
        age = now - published
        if age < dt.timedelta(0) or age > MAX_NEWS_AGE:
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id')}"
        haystack = f"{title} {url}".lower()
        if not any(re.search(rf"\b{re.escape(term)}\b", haystack) for term in RELEVANCE_TERMS):
            continue
        if any(existing["url"] == url or existing["title"].casefold() == title.casefold() for existing in accepted):
            continue
        accepted.append({
            "title": title,
            "url": url,
            "source": "Hacker News",
            "published_at": published.isoformat(),
            "age_hours": round(age.total_seconds() / 3600, 1),
            "freshness": "accepted",
        })
    return accepted


def collect() -> dict[str, Any]:
    generated = dt.datetime.now(dt.timezone.utc)
    result: dict[str, Any] = {
        "generated_at": generated.isoformat(),
        "status": "ok",
        "weather_source": WEATHER_URL,
    }
    # Weather is intentionally fetched by the cron agent with web_extract.
    # Keeping this URL in the structured payload preserves the exact-source contract.
    try:
        news = collect_news(now=generated)
        result["news_candidates"] = news
        if not news:
            result["news_status"] = "NO_VERIFIED_FRESH_HEADLINE"
    except Exception as exc:
        result["news_candidates"] = []
        result["news_status"] = "NO_VERIFIED_FRESH_HEADLINE"
        result["news_error"] = type(exc).__name__
    return result


def main() -> int:
    json.dump(collect(), sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
