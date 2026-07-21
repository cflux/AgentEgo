#!/usr/bin/env python3
import datetime as dt
import json
import unittest
from unittest.mock import Mock

import morning_briefing_collector as collector


class CollectorTests(unittest.TestCase):
    def test_weather_includes_today_forecast(self):
        payload = {
            "current_condition": [{
                "temp_F": "66", "FeelsLikeF": "65", "humidity": "72",
                "weatherDesc": [{"value": "Partly cloudy"}],
            }],
            "weather": [{
                "date": "2026-07-20", "maxtempF": "78", "mintempF": "57",
                "totalSnow_cm": "0.0",
                "hourly": [
                    {"chanceofrain": "20", "precipMM": "0.1", "weatherDesc": [{"value": "Cloudy"}]},
                    {"chanceofrain": "60", "precipMM": "1.4", "weatherDesc": [{"value": "Patchy rain"}]},
                ],
            }],
        }
        result = collector.parse_weather(payload)
        self.assertEqual(result["current"]["temp_f"], 66)
        self.assertEqual(result["today"]["high_f"], 78)
        self.assertEqual(result["today"]["low_f"], 57)
        self.assertEqual(result["today"]["max_precip_chance_percent"], 60)
        self.assertEqual(result["today"]["max_precip_mm"], 1.4)
        self.assertEqual(result["today"]["descriptions"], ["Cloudy", "Patchy rain"])

    def test_accepts_recent_story(self):
        now = dt.datetime(2026, 7, 20, 14, tzinfo=dt.timezone.utc)
        recent = int((now - dt.timedelta(hours=2)).timestamp())
        data = {"top": [1], "item": {"id": 1, "type": "story", "title": "Fresh AI security model", "url": "https://example.com", "time": recent}}
        result = collector.collect_news(now=now, get_json=lambda url: data["top"] if url == collector.HN_TOP_URL else data["item"])
        self.assertEqual(result[0]["freshness"], "accepted")
        self.assertEqual(result[0]["title"], "Fresh AI security model")

    def test_rejects_old_and_undated_stories(self):
        now = dt.datetime(2026, 7, 20, 14, tzinfo=dt.timezone.utc)
        data = {
            "top": [1, 2, 3],
            "items": {
                1: {"id": 1, "type": "story", "title": "Old", "time": int((now - dt.timedelta(days=3)).timestamp())},
                2: {"id": 2, "type": "story", "title": "No date"},
                3: {"id": 3, "type": "comment", "time": int(now.timestamp())},
            },
        }
        result = collector.collect_news(now=now, get_json=lambda url: data["top"] if url == collector.HN_TOP_URL else data["items"][int(url.rsplit("/", 1)[-1].split(".")[0])])
        self.assertEqual(result, [])

    def test_no_fresh_headline_is_explicit(self):
        now = dt.datetime(2026, 7, 20, 14, tzinfo=dt.timezone.utc)
        result = collector.collect_news(now=now, get_json=lambda url: [])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
