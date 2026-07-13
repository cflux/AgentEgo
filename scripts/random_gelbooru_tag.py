#!/usr/bin/env python3
"""
Gelbooru Roulette — Tag Picker
Shuffles and draws one tag from curated decks. Stacks SFW and explicit
tags into a mixed pool. 30% chance per draw of an explicit tag — keeps
the lineup varied without being the daily grind.
Edit the lists to curate what shows up.
"""
import random
import sys

# ─── SFW pool ─────────────────────────────────────────────────────────────────
TAGS_SFW = [
    # Landscapes & environments
    "scenery", "sky", "clouds", "sunset", "forest", "ocean", "mountains",
    "cityscape", "night_sky", "stars", "rain", "snow", "aurora",
    "ruins", "temple", "castle", "garden", "bridge", "lighthouse",
    "space", "planet", "nebula", "asteroid",

    # Architecture
    "architecture", "building", "street", "alley", "rooftop", "window",
    "futuristic_city", "cyberpunk", "fantasy_city", "steampunk",
    "japanese_castle", "european_town",

    # Creatures & characters
    "cat", "dog", "fox", "wolf", "dragon", "phoenix", "mecha", "robot",
    "elf", "knight", "samurai", "witch", "mage", "demon",
    "astronaut", "pirate", "cowboy", "warrior", "archer",

    # Aesthetics
    "neon", "cyberpunk", "vaporwave", "art_nouveau", "ukiyo-e",
    "watercolor", "oil_painting", "sketch", "pixel_art", "anime",
    "illustration", "concept_art",

    # Mood
    "melancholic", "serene", "epic", "cozy", "mysterious", "dramatic",
    "ethereal", "dreamy", "ominous", "whimsical",
]

# ─── Explicit pool (tag-level, not full-image filtering) ──────────────────────
TAGS_EXPLICIT = [
    "armor", "battle", "blood", "skull", "cannon", "sword",
    "magic", "spell", "summoning", "ritual",
    "chains", "cage", "cuffs", "leash",
    "demon_girl", "succubus", "angel_and_demon",
    "cyberpunk_girl", "mecha_girl", "android",
    "corruption", "transformation", "possession",
]


SFW_ONLY = "--sfw" in sys.argv


def _pick(nsfw_chance=0.30):
    """Draw one tag. ~30% explicit by default, suppress with --sfw."""
    if SFW_ONLY or random.random() >= nsfw_chance:
        return random.choice(TAGS_SFW), False
    pool = TAGS_SFW + TAGS_EXPLICIT
    return random.choice(pool), True


def pick(nsfw_chance=0.30):
    """Public entry point. Returns (tag, is_explicit)."""
    return _pick(nsfw_chance)


if __name__ == "__main__":
    tag, explicit = _pick()
    print(f"{tag}|{explicit}")
