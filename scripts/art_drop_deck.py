#!/usr/bin/env python3
"""
Daily AI Art Drop — Extended Component Deck
Shuffles and draws one card from each category to build a Krea 2 prompt skeleton.
NSFW subjects/styles are mixed in at random (roughly 25% chance per category).
Called by the Art Drop cron. Output: assembled prompt components to stdout.

Usage:
  python3 art_drop_deck.py          # SFW+NSFW mixed (default)
  python3 art_drop_deck.py --sfw    # SFW only
"""
import random
import sys

# ─── SFW POOLS ───────────────────────────────────────────────────────────────

MOODS_SFW = [
    "melancholy", "electric", "serene", "foreboding", "ethereal",
    "nostalgic", "ominous", "triumphant", "lonely", "chaotic",
    "dreamlike", "tense", "whimsical", "somber", "euphoric",
    "mysterious", "peaceful", "restless", "brooding", "hopeful",
    "defiant", "reverent", "carnival-like", "hypnotic", "primal",
    "wistful", "fervent", "languid", "apocalyptic", "tender",
    "delirious", "stoic", "radiant", "unsettling", "cozy",
    "electric", "giddy", "hollow", "rapturous", "stoic",
]

SUBJECTS_SFW = [
    # Architecture & Places
    "an abandoned space station drifting through a nebula",
    "a floating city above endless clouds, golden hour light",
    "a colossal stone archway carved into a living mountain",
    "a cathedral made of glass, suspended over a dark ocean",
    "a lighthouse on an asteroid, beam cutting through space",
    "a night market in a city built inside a giant tree",
    "a hidden village inside a dormant volcano, lit by magma",
    "a bridge made of light connecting two floating islands",
    "an observatory on a cliff during a meteor storm",
    "a clockwork tower where every gear is a different color of brass",
    "a throne room carved from a single massive crystal",
    "a library at the bottom of the ocean, fish swimming between shelves",
    "a train station that exists between dimensions",
    "a greenhouse on Mars, Earth visible through the glass",
    "an amphitheater carved into a cliff face overlooking a stormy sea",
    "a crumbling castle being slowly reclaimed by a crystal forest",
    "a spiral staircase descending into an impossibly deep well",
    "a city built on the back of a sleeping colossus",

    # Nature & Landscapes
    "a desert where the sand is made of crushed mirrors",
    "a frozen lake that reflects a different sky than the one above",
    "a canyon where the rock walls are carved with ancient star maps",
    "a forest where the trees are made of stained glass",
    "a waterfall flowing upward into a tear in the sky",
    "a field of flowers that bloom only in moonlight",
    "a cave system lit by pools of liquid starlight",
    "a mountain range where each peak is a fossilized giant",

    # Characters & Scenes
    "a lone figure on a rain-soaked neon bridge at 3am",
    "a cyberpunk street vendor selling holographic flowers",
    "a robot tending a garden of bioluminescent plants",
    "a diver exploring the ruins of a sunken megacity",
    "a hot air balloon race through canyons of purple rock",
    "a café at the edge of reality, patrons from every timeline",
    "a subway car overgrown with bioluminescent fungi",
    "a forge where stars are being hammered into shape",
    "an ancient library where the books whisper to each other",
    "a samurai standing at the edge of a data stream waterfall",
    "a street musician playing to an empty plaza at dawn, holographic notes floating away",
    "a witch tending her rooftop garden of carnivorous plants under twin moons",
    "a cartographer mapping a coastline that keeps changing shape",
    "a mechanic repairing a giant mech in a rain-soaked hangar",
    "a fortune teller in a neon-lit alley, cards floating mid-air",
    "a lone astronaut on a derelict ship, Earth's reflection in the visor",
    "a barista at a coffee shop that exists in the gap between seconds",
    "a lighthouse keeper discovering that the light is actually a living creature",
    "a detective in a trench coat standing under a flickering streetlamp",
    "a child flying a kite made of aurora light in an endless field",

    # Weird & Wonderful
    "a tea ceremony between a human and a sentient gas cloud",
    "the moment a dragon egg cracks open and releases a galaxy",
    "a chess game between cosmic entities using planets as pieces",
    "a memory restoration clinic where emotions are extracted as visible light",
    "the last tree on Earth, encased in a glass dome, tended by robots",
    "a jazz club on the event horizon of a black hole",
    "a library where each book is a captured dream in a glass jar",
    "a vending machine that dispenses bottled weather",
    "an art gallery where the paintings are windows into parallel timelines",
    "a signal tower broadcasting music to an empty universe",
]

STYLES_SFW = [
    "cinematic keyframe, wide aspect ratio, dramatic lighting",
    "oil painting on canvas, thick impasto brushstrokes, gallery quality",
    "anime key visual, crisp linework, cel-shaded with rim lighting",
    "brutalist architectural sketch, charcoal and graphite, moody shadows",
    "vaporwave aesthetic, synthwave palette, chromatic aberration",
    "watercolor and ink, loose washes, dreamy atmosphere",
    "sci-fi concept art, matte painting quality, epic scale",
    "film noir, high contrast black and white, venetian blind shadows",
    "art nouveau poster, flowing organic lines, gold leaf accents",
    "cyberpunk street photography, candid shot, natural framing",
    "Japanese woodblock print, ukiyo-e style, flat perspective",
    "holographic display, translucent layers, depth of field",
    "storybook illustration, warm palette, soft edges, magical realism",
    "industrial design concept, clean lines, studio lighting, product shot",
    "gothic stained glass window, backlit, cathedral glow",
    "Art Deco travel poster, geometric patterns, rich golds and teals",
    "pixel art, 16-bit era, limited palette, chunky charm",
    "biomechanical sketch, H.R. Giger-inspired, dark chrome and bone",
    "surrealist collage, Dali-esque, melting forms, dream logic",
    "street art mural, spray paint texture, urban decay canvas",
    "children's book illustration, soft pastels, gentle whimsy",
    "technical blueprint, annotated, cyanotype, architectural precision",
    "glitch art, corrupted data aesthetic, fragmented and beautiful",
    "1970s sci-fi paperback cover, pulp illustration, dramatic composition",
    "pointillism, Seurat-inspired, dots of pure color, luminous",
    "claymation still, textured, tactile, Aardman-esque charm",
    "fashion editorial, high contrast, dramatic pose, magazine quality",
    "celestial map, medieval manuscript style, gold leaf on deep blue",
    "thermal imaging, heat signature palette, scientific but beautiful",
    "cubist portrait, fractured planes, multiple perspectives, Picasso-inspired",
]

LIGHTING_SFW = [
    "golden hour, warm rim light, long shadows",
    "neon only, no natural light, heavy color casts",
    "bioluminescent glow from below, soft blue-green",
    "harsh industrial fluorescents, cold white, sharp shadows",
    "moonlight through clouds, silver and cool blue",
    "sunset through smoke, amber and deep red",
    "underwater light rays, dappled and shifting",
    "candlelit, warm orange pools of light, deep darkness beyond",
    "lightning strike, split-second white flash, frozen motion",
    "aurora borealis, shifting green and purple, diffuse",
    "firelight, flickering warm tones, dancing shadows",
    "total darkness save for a single point of light",
    "eclipse light, silver corona, everything edged in cold fire",
    "strobe, snapshot aesthetic, frozen mid-motion, harsh",
    "projector beam through haze, volumetric rays, cinematic",
    "morning mist, soft diffusion, everything wrapped in white",
    "lava glow from fissures below, orange and black, hellish warmth",
    "kaleidoscope of colored gels, theatrical, stage-lit",
    "bounce light off water, rippling caustics on every surface",
    "nuclear dawn, blinding white horizon, silhouettes only",
    "firefly swarm, thousands of pinprick lights, organic constellation",
    "neon sign buzz, pink and cyan, Blade Runner streets",
    "light painting, long exposure, trails of light in darkness",
    "god rays through stained glass, colored shafts of light, cathedral",
    "embers drifting upward, orange sparks against infinite black",
]

COMPOSITIONS_SFW = [
    "wide establishing shot, rule of thirds, deep depth of field",
    "dutch angle, off-kilter, sense of unease",
    "extreme close-up, shallow depth of field, intimate",
    "over-the-shoulder shot, voyeuristic, spatial depth",
    "low angle, hero perspective, towering scale",
    "symmetrical composition, centered, monumental",
    "leading lines drawing the eye to a distant focal point",
    "bird's eye view, top-down, miniature scale effect",
    "frame within a frame, doorway or window, layered depth",
    "diagonal composition, dynamic, sense of motion",
    "negative space dominant, subject small, isolation",
    "fisheye distortion, wide angle, immersive",
    "reflection shot, subject seen in water or glass, doubled",
    "silhouette, shape-only, mystery and drama",
    "tilted horizon, world feels off-balance, disorientation",
    "split-screen diptych, two moments in one frame, contrast",
    "worm's eye view, looking straight up, vertigo",
    "tracking shot blur, motion smear, speed",
    "macro detail, one element fills the frame, abstraction",
    "long shadow, subject dwarfed by their own shadow, dramatic",
]

# ─── NSFW POOLS ──────────────────────────────────────────────────────────────

MOODS_NSFW = [
    "sensual", "intimate", "yearning", "sultry", "passionate",
    "vulnerable", "electric", "tender", "raw", "smoldering",
]

SUBJECTS_NSFW = [
    # Tasteful / artistic nude
    "a figure silhouetted against a vast sunset, fabric falling away, tasteful and atmospheric",
    "two lovers embracing in zero gravity, hair and limbs floating, ethereal and intimate",
    "a dancer frozen mid-spin, body painted in gold, minimalist stage, artistic nude study",
    "a woman reclining on silk sheets, morning light through sheer curtains, renaissance pose",
    "a figure emerging from dark water, rivulets catching moonlight, classic fine art nude",
    "a couple in a rain-soaked alley, pressed against a neon-lit wall, steam rising, cinematic",
    "a warrior at rest, battle-worn and bare, scars like constellations, chiaroscuro lighting",
    "a mermaid on a rocky shore at dawn, human lover beside her, mythological romance",
    "a backlit figure behind frosted glass, hand pressed to the pane, longing, abstract",
    "an intimate moment in a bathhouse, steam and candlelight, classical composition",

    # Cyberpunk / sci-fi NSFW
    "a chromed-up netrunner in a private booth, cables trailing, lost in digital ecstasy",
    "a synth in a neon-drenched love hotel, glowing seams visible through translucent skin",
    "an edgerunner and her output post-gig, adrenaline still high, chrome catching amber light",
    "a holographic dancer in a private club, light passing through her form, otherworldly",
    "a mechanic in a grease-stained tank top, leaning over a hot rod engine, pin-up style",

    # Fantasy NSFW
    "an elven couple in a hidden forest pool, bioluminescent flora, waterfall mist, intimate",
    "a sorceress in her tower, spellbook open, robe slipping, candlelit, occult romance",
    "a vampire and their willing thrall, gothic bedroom, blood-red velvet, dangerous desire",
    "a selkie shedding their skin on a moonlit beach, caught between forms, vulnerable",
    "an incubus and a mortal in a dreamscape, reality bending around them, surreal passion",

    # Pin-up / boudoir
    "a 1950s pin-up mechanic, leaning over a cherry-red engine, wrench in hand, mischievous grin",
    "a burlesque performer backstage, mirror reflection, half-dressed, vintage Hollywood",
    "a femme fatale in a noir detective's office, blinds casting striped shadows, dangerous elegance",
    "a cowboy leaning in a saloon doorway, shirt unbuttoned, sunset behind, Western romance",
    "a rock star in a trashed hotel room, guitar in lap, morning after, raw and real",
]

STYLES_NSFW = [
    "classical oil painting, Caravaggio chiaroscuro, museum-quality nude study",
    "pulp romance novel cover, Fabio-era, dramatic embrace, wind-blown",
    "Art Nouveau erotic illustration, Klimt-inspired gold leaf and sensuality",
    "shoujo manga style, soft focus, cherry blossoms, romantic tension",
    "black and white fine art photography, Helmut Newton-inspired, powerful and provocative",
    "vintage pin-up illustration, Gil Elvgren style, playful and teasing",
    "Baroque painting, rich velvets and dramatic lighting, Rubenesque",
    "neon-soaked cyberpunk, rain on glass, Blade Runner 2049 intimacy",
]

LIGHTING_NSFW = [
    "candlelit, warm orange pools of light, deep darkness beyond, intimate",
    "morning light through sheer curtains, soft and hazy, golden and gentle",
    "neon sign buzz through a window, pink and cyan stripes across skin",
    "single spotlight, theatrical, everything else in shadow, dramatic",
    "firelight flickering, warm tones, dancing shadows on bare skin",
    "moonlight through rain-streaked glass, cool silver, quiet and private",
    "red darkroom glow, tinted shadows, tactile and secret",
    "sunset through a dusty window, long amber rays, nostalgic warmth",
]

COMPOSITIONS_NSFW = [
    "implied nudity, tasteful framing, suggestion over revelation",
    "reflection in a mirror, voyeuristic, layered depth, private moment",
    "extreme close-up on hands touching, intimate detail, skin texture",
    "silhouette against bright light, form defined by shadow, mystery",
    "dutch angle, off-kilter, heightened emotion, cinematic romance",
    "negative space dominant, subjects small in frame, atmosphere over explicitness",
    "backlit figure, rim light defining curves, everything else dark",
    "pov shot, intimate first-person, drawing the viewer into the moment",
]


# ─── DRAW LOGIC ───────────────────────────────────────────────────────────────

SFW_ONLY = "--sfw" in sys.argv


def _maybe_mix(sfw_pool, nsfw_pool, nsfw_chance=0.25):
    """Return sfw_pool + nsfw_pool if not SFW_ONLY, and randomly pick from the merged pool."""
    if SFW_ONLY:
        return random.choice(sfw_pool)
    # Occasionally lean into an NSFW card
    if random.random() < nsfw_chance and nsfw_pool:
        return random.choice(sfw_pool + nsfw_pool)
    return random.choice(sfw_pool)


def draw():
    """Draw one card from each deck."""
    return {
        "mood": _maybe_mix(MOODS_SFW, MOODS_NSFW),
        "subject": _maybe_mix(SUBJECTS_SFW, SUBJECTS_NSFW),
        "style": _maybe_mix(STYLES_SFW, STYLES_NSFW),
        "lighting": _maybe_mix(LIGHTING_SFW, LIGHTING_NSFW),
        "composition": _maybe_mix(COMPOSITIONS_SFW, COMPOSITIONS_NSFW),
    }


def assemble(components):
    """Turn drawn cards into a Krea 2 natural-language prose prompt."""
    return (
        f"{components['subject']}. "
        f"The mood is {components['mood']}. "
        f"Style: {components['style']}. "
        f"Lighting: {components['lighting']}. "
        f"{components['composition']}."
    )


if __name__ == "__main__":
    cards = draw()
    print(assemble(cards))
