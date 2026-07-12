#!/usr/bin/env python3
# Output a random subreddit from a varied curated list
import random

# SFW — fun, interesting, beautiful
SFW = [
    "mildlyinteresting", "interestingasfuck", "Damnthatsinteresting",
    "funny", "pics", "EarthPorn", "itookapicture", "art",
    "DesignPorn", "cozyplaces", "oddlysatisfying", "BeAmazed",
    "interesting", "mostbeautiful", "woahdude", "gifs",
    "perfecttiming", "AccidentalRenaissance", "NatureIsFuckingLit",
    "spaceporn", "AbandonedPorn", "CityPorn", "RoomPorn",
    "whatsthisbug", "cats", "dogpictures", "aww", "Eyebleach",
    "foodporn", "sushi", "steak", "WeWantPlates",
    "imaginarylandscapes", "PixelArt", "trippy", "glitch_art",
    "PropagandaPosters", "MapPorn", "dataisbeautiful",
    "OldSchoolCool", "TheWayWeWere", "VintageMenus",
    "crappyoffbrands", "mildlyinfuriating", "ATBGE",
    "DiWHY", "ExpectationVsReality",
    "cyberpunk", "outrun", "ImaginaryTechnology", "ImaginaryCyberpunk",
    "ProgrammerHumor", "mechanicalkeyboards", "battlestations",
    "somethingimade", "woodworking", "3Dprinting", "DIY",
    "miniatures", "tiltshift", "generative", "fractals",
    "evilbuildings", "brutalism", "astrophotography", "CLOUDS",
    "megalophobia", "LiminalSpace", "AnimeFigures",
]

# NSFW — female nudity, no gore/bio
NSFW = [
    "rule34", "gonewild", "workplace_gw", "HENTAI_GIF", "ArtGW", "classysexy", "gentlemanboners", "prettygirls",
]

# Mix: 80% SFW, 20% NSFW
if random.random() < 0.2:
    print(random.choice(NSFW))
else:
    print(random.choice(SFW))
