#!/usr/bin/env python3
"""
Bizarre Product Roulette — Subreddit Picker
Edit the list to add/remove subreddits. Prints a random one to stdout.
"""
import random

SUBREDDITS = [
    "AmazonWTF",
    "WTF_Amazon",
    "CrackheadCraigslist",
    "ATBGE",
    "DiWHY",
    "CrappyDesign",
    "ExpectationVsReality",
]

if __name__ == "__main__":
    print(random.choice(SUBREDDITS))
