#!/usr/bin/env python3
"""
portrait_generator.py - turns what Portrait Scout knows about a creature into a prompt and asks an image
generator for candidate portraits.

It is separate from Portrait Scout on purpose. Portrait Scout only calls:

    describe(row, settings)                  -> (subject, character)   text worked out from the game data
    build_prompt(settings, subject, character, extra) -> (prompt, negative prompt)
    Engine(settings).generate(prompt, negative, count, on_image)       -> PNG images

Everything about the LOOK (prompt template, negative prompt, model, LoRA, sampler, size) is in settings.json next
to this file - edit that, not the code. The engine is anything that speaks the Automatic1111 / Forge web API
(POST /sdapi/v1/txt2img). By default that is Portrait Scout's own copy of Forge in ../engine, started in the
background by start_engine.bat; point "url" at another server to use your own instead.

Only the Python standard library is used.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import hashlib
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "settings.json"

DEFAULTS: dict = {
    "_about": "Portrait generator settings. No image generator and no models come with Portrait Scout: this file says "
              "which one to talk to (engine) and what to ask it for (styles, payload). {subject} = short description "
              "(e.g. 'female elf'); {character} = the full description plus your extra details. Change the look here; "
              "the code never needs editing.",
    "prompt": "(massive head framing:1.6), (head fills 70 percent of frame:1.7), macro face framing, (tight zoom:1.5), "
              "(textured single color warm solid backdrop:1.2), weathered appearance, matte finish, cool directional "
              "lighting, head and shoulders, digital painting, D&D character portrait, front view portrait, centered "
              "framing, head and shoulders, fantasy digital illustration, stylized digital painting, semi-realistic "
              "character art, smooth shading, vibrant colors, detailed eyes, dramatic cinematic rim lighting, shallow "
              "depth of field, crisp focus, artstation trending, {subject}, <lora:Baldurs_Gate_Portrait_Flux:.55>, "
              "A detailed digital fantasy character portrait, dappled sunlight, rich vibrant colors, detailed skin "
              "texture and pores, set against {background}. Clean digital painting "
              "style, crisp facial details, dramatic rim lighting, {character}",
    "negative_prompt": "(monochrome skin:1.4), (sepia skin:1.4), (brown skin tint:1.3), (black background:1.4), "
                       "(dark black backdrop:1.4), (glossy:1.5), (digital art:1.4), (smooth skin:1.4), (3d render:1.5), "
                       "(photograph:1.5), (picture frame:1.6), (border:1.6), (framed picture:1.6), (torso:1.6), "
                       "(abs:1.5), (belt:1.5), (chest armor:1.6), (waist up:1.5), (medium shot:1.5), (bust shot:1.4), "
                       "bare chest, bare shoulders, nude, wide shot, zoomed out, distant shot, far away, anime, "
                       "modern clothes, shiny, eyeglasses, signature",
    "background": "a softly blurred, out-of-focus painterly wash of {random} with soft bokeh highlights",
    "_background": "What is behind the head. The prompts put it in at {background}, so one change covers every "
                   "look; a style can also carry a 'background' of its own. {random} here is replaced by one of "
                   "background_palettes, picked fresh for each picture.",
    "background_palettes": [
        "muted warm brown and clay",
        "aged canvas and parchment, cream and tan",
        "dull ochre and soft gray-brown",
        "faded red-brown and deep umber",
        "smoky gold and warm brown",
        "dusty olive and warm gray",
        "deep green-gray and dull ochre",
        "muted teal and warm shadow",
        "desaturated slate blue and pale stone",
        "muted indigo and bone white",
        "dull brick red and dark umber",
        "deep moss green and weak amber",
    ],
    "_background_palettes": "Color schemes for the background, used wherever the background text says {random}. "
                            "One is picked at random per picture so a few thousand portraits do not all sit "
                            "against the same wash. All of them are deliberately muted and weighted towards earth "
                            "colors, the way the game's own portraits are - a bright background pulls the eye "
                            "off the face. Add or remove freely; take {random} out of the background text to go "
                            "back to one fixed scheme.",
    "style": "BG2 oil painting",
    "styles": {
        "_about": "Named looks to choose from in the Generate window. Each has its own 'prompt' (and optionally "
                  "'negative_prompt'); 'Current look' is the prompt above. Add your own here.",
        "BG2 framing": {
            "_about": "Needs the larger 292 MB LoRA (Baldur's Gate 1-2 Portrait Style), which is an extra download. "
                      "Head filling the frame, the way Baldur's Gate portraits (and the HPS portrait project) are "
                      "framed, so the picture needs little "
                      "or no cropping afterwards (zoom 1). If faces still come out small, raise the LoRA weight "
                      "(:.65 -> :1) or crop with Head zoom. Its trigger word 'b2g8' is left out on purpose: it pulls hard "
                      "towards the faces it was trained on and drops things like a tiefling's horns.",
            "zoom": 1.0,
            "prompt": "Extreme close-up face portrait of {character}, cropped at the collarbone. The head alone "
                      "fills the picture: the hair nearly touches the top edge, the face reaches from edge to edge, "
                      "the chin sits in the middle, and only a sliver of collar and shoulder shows along the bottom. "
                      "Nothing below the collarbone, no chest, no armor plates, no hands, no weapons. Head upright, "
                      "eyes on the viewer. Painted as a classic Baldur's Gate character portrait: a realistic oil "
                      "painting with visible brushstrokes and canvas texture, realistic facial proportions, natural "
                      "skin tones, detailed weathered skin, expressive lifelike eyes, deep rich colors, warm light "
                      "from one side with soft shadows. <lora:Baldurs_Gate_Portrait_Flux:.65> Behind the head, "
                      "{background}."
        },
        "BG2 oil painting": {
            "_about": "The best Flux result worked out against the HPS portraits. Short on purpose: Flux weights "
                      "the opening of a prompt hardest, so the character and the clothing come first and the "
                      "painting instructions after. The lighting sentence that older styles carried ('warm light "
                      "from one side') is left out - it was what put the sheen back on the forehead. Wants "
                      "distilled_cfg_scale about 1.6; above 2 the skin goes glossy. Needs "
                      "chargen_oil_painting_v2.safetensors in models/Lora.",
            "zoom": 1.0,
            "prompt": "in the style of chargen_oil_painting, extreme close-up face portrait of {character}. "
                      "Painted in thick matte oils: broken color, visible brush edges, dry-brush texture, no "
                      "blending, no smooth skin, no sheen. The head fills most of the frame, cropped at the chest, "
                      "chin level, eyes on the viewer, shoulders and collar fully clothed. "
                      "<lora:chargen_oil_painting_v2:.85> Behind the head, {background}, kept off the face.",
        },
        "BG2 oil painting, long": {
            "_about": "The longer wording this style had before: the same idea said at three times the length. "
                      "Kept in case the short one is too terse for a character who needs a lot said about them; "
                      "it frames looser and puts more shine on the skin.",
            "zoom": 1.0,
            "prompt": "in the style of chargen_oil_painting, extreme close-up face portrait of {character}, "
                      "cropped at the collarbone. The head alone fills the picture: the hair nearly touches the "
                      "top edge, the face reaches from edge to edge, the chin sits in the middle, and only a "
                      "sliver of collar and shoulder shows along the bottom. Nothing below the collarbone, no "
                      "chest, no hands, no weapons. Head upright, eyes on the viewer. A classic Baldur's Gate "
                      "character portrait painted in oils: the face painted with the same loose brushwork as the "
                      "clothing - broken color, visible brush edges on the cheek and brow, no smooth blending - "
                      "the skin mottled and uneven, flecks, blotches and stubble worked into the paint rather "
                      "than blended away. Matte paint, no glossy highlights on the skin, the shine on metal only. "
                      "Thick paint, canvas texture, deep rich colors. <lora:chargen_oil_painting_v2:.85> Behind "
                      "the head, {background}.",
        },
        "BG2 framing, light LoRA": {
            "_about": "The same framing on the small BG2 LoRA (BG_Portrait, 38 MB) that the setup notes point to. "
                      "It nudges the style instead of taking it over, so the description is followed more closely.",
            "zoom": 1.0,
            "prompt": "Extreme close-up face portrait of {character}, cropped at the collarbone. The head alone "
                      "fills the picture: the hair nearly touches the top edge, the face reaches from edge to edge, "
                      "the chin sits in the middle, and only a sliver of collar and shoulder shows along the bottom. "
                      "Nothing below the collarbone, no chest, no armor plates, no hands, no weapons. Head upright, "
                      "eyes on the viewer. Painted as a classic Baldur's Gate character portrait: a realistic oil "
                      "painting with visible brushstrokes and canvas texture, realistic facial proportions, natural "
                      "skin tones, detailed weathered skin, expressive lifelike eyes, deep rich colors, warm light "
                      "from one side with soft shadows. <lora:BG_Portrait:.8> Behind the head, {background}."
        },
        "Painterly": {
            "_about": "Closer than the HPS portraits: crop it with Head zoom 1.25 or so.",
            "zoom": 1.0,
            "prompt": "Close-up head and shoulders portrait of {character}. The face fills most of the frame, front "
                      "view, centered. Painterly fantasy character portrait in the manner of classic Dungeons & Dragons "
                      "book illustrations and Baldur's Gate portraits: a realistic oil painting with visible brushstrokes "
                      "and rich canvas texture, realistic facial proportions, natural skin tones, detailed weathered "
                      "skin, expressive lifelike eyes, deep rich colors, warm light from one side with soft shadows. "
                      "<lora:Baldurs_Gate_Portrait_Flux:.55> Set against {background}."
        },
        "Painterly, lighter LoRA": {
            "zoom": 1.0,
            "prompt": "Close-up head and shoulders portrait of {character}. The face fills most of the frame, front "
                      "view, centered. Painterly fantasy character portrait in the manner of classic Dungeons & Dragons "
                      "book illustrations and Baldur's Gate portraits: a realistic oil painting with visible brushstrokes "
                      "and rich canvas texture, realistic facial proportions, natural skin tones, detailed weathered "
                      "skin, expressive lifelike eyes, deep rich colors, warm light from one side with soft shadows. "
                      "<lora:Baldurs_Gate_Portrait_Flux:.3> Set against {background}."
        },
    },
    "candidates": 2,
    "payload": {
        "_about": "Sent to the engine as is (Forge / Automatic1111 txt2img fields).",
        "steps": 25, "sampler_name": "DPM++ 2M", "scheduler": "SGM Uniform", "cfg_scale": 1, "distilled_cfg_scale": 1.6,
        "width": 656, "height": 1024, "seed": -1, "batch_size": 1,
        "override_settings": {"sd_model_checkpoint": "flux1-dev-bnb-nf4-v2.safetensors"},
    },
    "engine": {
        "_about": "No generator and no models come with Portrait Scout. url = where the generator's API is "
                  "(anything that speaks the Automatic1111 / Forge API). If nothing answers there and autostart is "
                  "true, 'start' is run (from the Portrait Scout folder; {port} is taken from url) and stopped again "
                  "when Portrait Scout closes. To use a generator you already run, set autostart to false and put its "
                  "address in url; 'start' is then never used.",
        "url": "http://127.0.0.1:7861",
        "autostart": True,
        "show_window": False,
        "_show_window": "true: the generator gets a window of its own, so you can watch what it prints (and nothing "
                        "is written to engine.log, because it goes to that window instead).",
        "start": "generator\\start_engine.bat {port}",
        "start_timeout_seconds": 600,
        "image_timeout_seconds": 900,
    },
    "bare_races": ["sahuagin", "troll", "lizard man", "gnoll", "kobold"],
    "_bare_races": "Creatures whose sprite wears nothing a portrait would show. The class in a creature file "
                   "says only which animation was borrowed, so 'fighter, worn armor' on one of these dresses a "
                   "beast as a soldier and pulls the picture toward a man in armor. Their race term says what "
                   "they wear, if anything. Races not listed here keep the class and clothing.",
    "face_detail": "off",
    "_face_detail": "How much of a made-up face to describe. full: shape, nose, hair and sometimes a mark, as "
                    "before. light: hair only - the engine paints its own nose whatever is asked for, so the "
                    "rest is words it ignores and tokens that dilute what it does read. off: nothing. Two "
                    "creatures still look different either way: the face is drawn from the creature's own seed, "
                    "not from these words.",
    "use_creature_colors": True,
    "_use_creature_colors": "The creature file says what color its sprite's clothes, hair and skin are. true: "
                             "those words go into the description, so the portrait matches the figure walking "
                             "around. false: leave them out and let the look decide.",
    "ghost_fade": 0.55,
    "_ghost_fade": "How far a ghostly creature is faded into its own background once the picture is made, from 0 "
                   "(not at all) to 1.5. Past 1 the dissolve starts higher up the face and the mist thickens. The engine will not paint a see-through person whatever the description "
                   "says, so the picture is faded afterwards: solid at the brow, gone by the bottom, with the "
                   "eyes left burning. Switched on by itself for spectres, ghosts and wraiths.",
    "output_folder": "generated",
    "portrait_bmp": {
        "_about": "Size of the .bmp made when you press 'Use this' (the picture is cropped to this shape from the "
                  "middle, then shrunk). 169x266 is the size the HPS portrait project uses for speakers.",
        "width": 169, "height": 266,
        "zoom": 1.0, "top": 0.0,
        "_zoom": "zoom = how much closer the .bmp is than the generated picture (1 = whole picture, 1.25 = a quarter closer, "
                 "which matches the framing of the HPS portraits). top = where the crop starts, as a fraction of the picture's height "
                 "(0 = very top). The crop is centered left to right.",
    },
    "age_terms": {
        "_about": "Offered as 'Age' in the Generate window; the text goes into the description.",
        "Young": "young", "Adult": "adult", "Middle-aged": "middle-aged",
        "Old": "old, lined face, gray hair", "Ancient": "very old, deeply wrinkled, thin white hair",
    },
    "look_terms": {
        "_about": "Offered as 'Look' in the Generate window; the text is added to the end of the description. "
                  "Flux paints clean skin unless it is told otherwise, so these are worth using - and the "
                  "blemishes have to be named one by one, because 'weathered' on its own is not enough.",
        "Ordinary": "",
        "Weathered": "a deeply lined weather-beaten face, furrowed brow and heavy crow's feet, sun-darkened "
                     "uneven skin with visible pores and broken veins across the nose and cheeks, gray-flecked "
                     "stubble, shadowed eye sockets, dry chapped lips",
        "Blotchy and freckled": "blotchy ruddy skin heavily freckled across the nose and cheeks, patches of red "
                                "and sallow color, flaking dry skin, pitted pores, uneven stubble, yellowed "
                                "crooked teeth",
        "Gaunt and old": "a gaunt deeply wrinkled old face, hollow cheeks and sunken temples, sagging skin, liver "
                         "spots and broken veins, heavy eye bags, thin gray hair and straggly beard, sallow "
                         "weathered color",
        "Scarred": "an old scar across the face, rough pitted skin",
        "Stern": "a stern, hard expression",
        "Friendly": "a warm, friendly expression, smiling eyes",
        "Sad": "a tired, downcast expression",
        "Fierce": "a fierce, angry expression, bared teeth",
        "Angry": "an angry, scowling expression, furrowed brow",
        "Sickly": "pale, gaunt and unwell, gray blotchy skin, dark rings under the eyes",
        "Noble": "clean, well-kept, proud bearing",
    },
    "bald_animations": ["fat"],
    "_bald_animations": "Words from the creature's animation name whose sprite is bald (the 'Fat Man' sprite "
                        "several merchants use). These win over the creature file, which can give a hair color "
                        "to a creature whose sprite has no hair. Anything else with a hair color is never drawn "
                        "bald. Minsc is the other way round - bald in the art, hair in his file - so a creature "
                        "the game draws bald without using one of these sprites has to be typed in by hand.",
    "feature_terms": {
        "_about": "Drawn from the creature's own file name, so the same creature always gets the same face and "
                  "two creatures with the same name and class do not. One is taken from each list below and added "
                  "to the description; 'mark' is used only some of the time (feature_mark_chance). Edit the lists "
                  "freely - anything you write in the Description box yourself is used as it is and overrides "
                  "none of this.",
        "face": [
            "a broad square face",
            "a long narrow face",
            "a round face",
            "a heavy-jawed face",
            "a fine-boned face",
            "a flat wide face",
            "a gaunt hollow-cheeked face",
            "a soft jowly face",
            "an oval face",
            "a high-cheekboned face",
            "a square-jawed face",
            "a narrow face with a weak chin",
            "a plain ordinary face",
            "a broad open face",
            "a lean weathered face",
        ],
        "nose": [
            "a straight nose",
            "a long narrow nose",
            "a short straight nose",
            "a round nose",
            "a wide flat nose",
            "a slightly upturned nose",
            "an aquiline nose",
            "a broad nose",
            "an unremarkable nose",
            "a hooked nose",
            "a broken flattened nose",
            "a snub nose",
            "a bulbous nose",
            "a thin pinched nose",
            "a crooked nose",
        ],
        "hair": [
            "close-cropped hair",
            "a receding hairline",
            "lank shoulder-length hair",
            "thick curly hair",
            "a bald head",
            "hair tied back",
            "a tangled unwashed mane",
            "a neat side parting",
            "short neat hair",
            "a widow's peak",
            "graying hair swept back",
            "braided hair",
            "cropped and balding",
            "wavy hair to the jaw",
            "a plain short cut",
        ],
        "mark": [
            "a mole high on one cheek",
            "a missing front tooth",
            "a cauliflower ear",
            "a split eyebrow",
            "a crooked jaw",
            "a drooping eyelid",
            "a notched ear",
            "a burn scar along the jaw",
            "one eye paler than the other",
            "a deep dent in the brow",
        ],
    },
    "feature_mark_chance": 0.35,
    "_feature_mark_chance": "How often a creature gets a distinguishing mark from feature_terms['mark'] (0 = "
                            "never, 1 = always). Kept low on purpose: a crowd where everyone has a scar looks as "
                            "uniform as a crowd where nobody does.",
    "race_terms": {
        "_about": "How a race from the game is worded in the prompt. Races not listed are used as they are, which is rarely enough for a monster: a bare \"orc\" is painted as a human with pointed ears, so say what makes one.",
        "human": "human",
        "elf": "elf",
        "half elf": "half-elf",
        "dwarf": "dwarf, stocky",
        "halfling": "halfling",
        "gnome": "gnome",
        "half orc": "half-orc, pale green skin, heavy brow, muscular",
        "sirine": "sirine, a sea fey woman, green skin, long dark hair wet and tangled with seaweed, large dark "
                  "eyes, a serene unsettling beauty",
        "doppleganger": "doppelganger, a half-formed gray humanoid with blank featureless eyes, smooth unfinished "
                        "features caught between faces",
        "imp": "imp, a small red devil, bat wings, little horns, a barbed tail, a sly pointed face, yellow eyes",
        "demonic": "demon, a monstrous fiend with a beast's head, horns, burning eyes, dark hide, bared fangs",
        "rakshasa": "rakshasa, a tiger's head on a man's shoulders, orange and black striped fur, amber eyes, "
                    "rich embroidered robes",
        "mind flayer": "mind flayer, a purple-gray octopus head with four writhing face tentacles, white pupilless "
                       "eyes, no nose",
        "beholder": "beholder, a floating sphere of flesh with one huge central eye and eyestalks writhing above "
                    "it, a wide toothed maw",
        "minotaur": "minotaur, a bull's head on a man's shoulders, heavy curved horns, a brass nose ring, "
                    "shaggy brown fur",
        "svirfneblin": "deep gnome, small and wiry, gray stone-colored skin, large black eyes, a bald head, a "
                       "wispy gray beard",
        "genie": "djinni, a towering blue-skinned spirit of the air, white eyes with no pupils, a pointed beard, "
                 "the lower body trailing away into smoke",
        "fairy": "fey creature, small and sharp-featured, bright inhuman eyes, pointed ears, a faint glow",
        "golem": "golem, a mute figure built of stone and iron, blank carved features, no eyes, seams and rivets",
        "mephit": "mephit, a small winged imp of elemental stuff, a mischievous pinched face, little bat wings",
        "myconid": "myconid, a walking fungus, a domed mushroom cap for a head, mottled spotted skin, tiny dark eyes",
        "gibberling": "gibberling, a small hunched shrieking creature, a mouth too wide, matted fur, wild eyes",
        "kuo-toa": "kuo-toa, a fish-headed humanoid, bulging silver eyes on the sides of the head, scaled gray "
                   "skin, a lipless mouth of needle teeth",
        "yuanti": "yuan-ti, a serpent-headed humanoid, green and gold scales, slit yellow eyes, a forked tongue",
        "solar": "solar, a towering winged celestial, burning golden eyes, skin like polished bronze, great "
                 "white wings",
        "planatar": "planetar, a tall winged celestial with smooth green-white skin, a bald head, white feathered "
                    "wings, pupilless eyes",
        "ogrillon": "ogrillon, a half-ogre brute, coarse gray-brown skin, a heavy misshapen jaw, small eyes",
        "wererat": "wererat, a rat's head on a man's shoulders, patchy brown fur, a long snout, yellow incisors, "
                   "pink ears",
        "revenant": "revenant, a corpse come back, gray tight skin, sunken burning eyes, old wounds unhealed",
        "ghast": "ghast, a ravening corpse, gray rotting skin, bared teeth, sunken eyes, lank clumps of hair",
        "spectre": "spectre, a ghostly undead spirit, hollow eyes lit from within, gaunt drawn face, "
                   "colorless bloodless skin, wisps of mist where the shoulders fade away",
        "spectral undead": "spectral undead, a ghostly spirit, hollow eyes lit from within, gaunt drawn face, "
                           "colorless bloodless skin, the edges fading into mist",
        "wraith": "wraith, a hooded shape of darkness, no face but two points of cold light, tattered shroud",
        "ghost": "ghost, a pale ghostly figure, hollow eyes, bloodless colorless skin, edges fading away",
        "shadow": "living shadow, a man-shaped darkness, two cold points of light for eyes",
        "vampire": "vampire, bloodless white skin, red eyes, long fangs, sunken cheeks",
        "ghoul": "ghoul, gray rotting skin stretched over the skull, sunken eyes, bared teeth, patchy hair",
        "zombie": "zombie, gray rotting skin, clouded eyes, slack jaw, matted hair",
        "skeleton": "skeleton, a bare skull and bones, empty eye sockets, no flesh at all",
        "lich": "lich, a skull face with shrivelled gray skin drawn tight over it, points of light in the sockets",
        "mummy": "mummy, dried blackened skin wrapped in filthy bandages, sunken sockets",
        "orc": "orc, green skin, a heavy jutting brow and small eyes, a broad flat nose, jutting lower tusks, pointed ears, coarse black hair",
        "goblin": "goblin, sickly green skin, huge pointed ears, a long hooked nose, small yellow eyes, sharp teeth",
        "hobgoblin": "goblin, orange-red skin, a flat broad face with a heavy jutting brow, dark beady eyes, a "
                     "blue-gray flattened nose, pointed ears, yellowed teeth, wearing a battered "
                     "horned rusty steel helmet",
        "kobold": "kobold, small reptilian head, rust-red scaled skin, a short snout, horns, beady red eyes",
        "gnoll": "gnoll, hyena head, coarse spotted fur, a long muzzle with bared teeth, dark eyes",
        "ogre": "ogre, huge lumpen head, dull yellow-brown skin, a heavy jaw and small eyes, tusks, wearing a "
                "battered horned iron helmet",
        "troll": "troll, long rubbery green head, a hooked nose, sunken eyes, lank hair",
        "lizard man": "lizard man, green scaled head, a blunt snout, slit yellow eyes, a crest of spines",
        "sahuagin": "sahuagin, green-black fish head, round black eyes, gill slits, needle teeth, a finned crest",
        "githyanki": "githyanki, gaunt yellow-skinned head, sunken black eyes, pointed ears, a hairless skull",
        "duergar": "gray dwarf, ashen gray skin, a bald head, a long gray beard, hard pale eyes",
        "tiefling": "devil-blooded human, small horns, unnatural skin",
        "drow": "drow elf, dark gray skin, white hair",
        "aasimar": "angelic human, fair luminous skin, pale golden eyes, a faint halo of light",
        "half ogre": "half-ogre, broad coarse face, dull ruddy skin, a heavy jaw, small tusks, wearing a "
                     "battered horned iron helmet",
        "lycanthrope": "werewolf, a wolf's head on a man's shoulders, coarse gray fur, yellow eyes, bared fangs",
    },
    "animation_terms": {
        "_about": "Words in the creature's animation name -> what the speaker wears. Portraits are head and "
                  "shoulders, so this is clothing, not held equipment, and each phrase is kept to a couple of "
                  "words: the more the prompt says about armor and gear, the further back the picture is framed.",
        "fighter": "fighter, worn armor",
        "paladin": "paladin, plate armor",
        "ranger": "ranger, green cloak",
        "cleric": "cleric, vestments",
        "druid": "druid, rough robes",
        "mage": "mage, high-collared robe",
        "sorcerer": "sorcerer, ornate robe",
        "thief": "rogue, hooded cloak",
        "bard": "bard, colorful clothes",
        "monk": "monk, plain robe",
        "barbarian": "barbarian, furs",
        "shaman": "shaman, bone charms",
        "peasant": "commoner, worn clothes",
        "noble": "noble, rich clothes",
        "beggar": "beggar, rags",
        "slave": "rags",
        "fat": "heavyset, round face",
        "guard": "town guard, mail",
        "merchant": "merchant, good clothes",
        "boy": "boy, simple clothes",
        "girl": "girl, simple clothes",
    },
}


class GeneratorError(Exception):
    """Something the user can fix (engine missing, not answering...). The message is shown as it is."""


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_settings() -> dict:
    """settings.json merged over the defaults; the file is written on first use so there is something to edit."""
    if not SETTINGS_FILE.exists():
        try:
            SETTINGS_FILE.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
        except OSError:
            pass
        return json.loads(json.dumps(DEFAULTS))
    try:
        had = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if "use_creature_colours" in had:                        # written before the spelling was settled
            had.setdefault("use_creature_colors", had.pop("use_creature_colours"))
        return _merge(DEFAULTS, had)
    except (OSError, ValueError) as e:
        raise GeneratorError(f"generator/settings.json could not be read: {e}") from e


# ------------------------------------------------------------------------------------------- description
_HUMANOID = {"human", "elf", "half elf", "dwarf", "halfling", "gnome", "half orc", "tiefling", "drow", "aasimar",
             "duergar", "genasi", "orc", "goblin", "kobold", "ogre", "gnoll", "hobgoblin"}


def term_names(settings: dict, which: str) -> List[str]:
    """The choices offered for 'age_terms' or 'look_terms', in the order they are written in settings.json."""
    return [k for k in (settings.get(which) or {}) if not k.startswith("_")]


def _face_key(row: dict) -> str:
    """What a creature's face is drawn from: its own creature file. A shared portrait is not proof of one person -
    a stock picture like FARM is worn by twenty unrelated commoners - so it is only used when there is no creature
    file at all, and the name only when there is neither."""
    cres = row.get("cre_list") or row.get("cres") or []
    if cres:
        return "c:" + str(cres[0]).upper()
    for k in ("large_name", "small_name"):
        v = str(row.get(k) or "").strip()
        if v:
            return "p:" + v.upper()
    nm = str(row.get("name") or "").strip().upper()
    return "n:" + nm if nm and nm != "ANY PORTRAIT" else ""        # the stand-in name is not a creature


def face_key(row: dict) -> str:                                    # noqa: F811 - the variant wraps the plain key
    """As _face_key, plus the creature's 'face_variant' when one has been asked for, so a face can be re-rolled
    and still be the same every time afterwards."""
    k = _face_key(row)
    v = str(row.get("face_variant") or "").strip()
    return f"{k}#{v}" if k and v not in ("", "0") else k


def creature_seed(key: str) -> int:
    """A seed that is the same every time for the same creature (python's hash() is salted per run, so not that)."""
    if not key:
        return -1
    return int(hashlib.md5(key.encode("utf-8", "replace")).hexdigest()[:8], 16)


_BALD = ("bald", "balding", "hairless")
_NAME_BLIND = frozenset(("shadow",))    # the Shadow Thieves are a guild of people, not forty-five living shadows
_VAGUE_RACES = frozenset(("human", "fairy", "demonic", "monster", "animal", "mammal", "no race", "undead",
                          "giant", "golem", "elemental", "statue"))
_COVERED_HEAD = ("helm", "hood", "coif", "cowl", "turban", "veil")
_ROLES = frozenset(("fighter", "paladin", "ranger", "cleric", "druid", "mage", "sorcerer", "thief", "bard", "monk",
                    "barbarian", "shaman", "peasant", "noble", "beggar", "slave", "guard", "merchant"))
_HAIR_COLOR = re.compile(r"\b(greying|graying|grey|gray|white|black|blond|blonde|red|auburn|brown|silver)\s+"
                          r"(?=hair|mane|curls|locks)", re.I)


def features(key: str, settings: dict, has_hair: bool = False, bald: bool = False) -> str:
    """One face, nose and hair for this creature, and sometimes a distinguishing mark - always the same ones.
    has_hair: the creature file gives a hair color, so it has hair - don't draw it bald.
    bald: its sprite is bald, whatever the file says - draw it bald.
    How much of it is said depends on face_detail: the engine paints the nose it wants whatever is asked for,
    so by default only the hair is sent. Everything is still drawn, so the detail setting never moves a face."""
    if not key:
        return ""
    detail = str(settings.get("face_detail") or "light").strip().lower()
    if detail == "off":
        return "a bald head" if bald else ""                  # a sprite with no hair is not a made-up feature
    terms = settings.get("feature_terms") or {}
    rng = random.Random(creature_seed(key))
    out = []
    for slot in ("face", "nose", "hair"):
        pool = [t for t in (terms.get(slot) or []) if str(t).strip()]
        if slot == "hair" and has_hair and not bald:
            kept = [t for t in pool if not any(w in str(t).lower() for w in _BALD)]
            pool = kept or pool                                  # unless bald is all there is
        if pool:
            pick = rng.choice(pool)                              # drawn either way, so the mark below is unmoved
            if detail == "full" or slot == "hair":               # light: the hair, which the engine does paint
                out.append("a bald head" if slot == "hair" and bald else pick)
    marks = [t for t in (terms.get("mark") or []) if str(t).strip()]
    try:
        chance = float(settings.get("feature_mark_chance", 0.35))
    except (TypeError, ValueError):
        chance = 0.35
    if marks and rng.random() < chance and detail == "full":  # a mole is a pixel across at portrait size
        out.append(rng.choice(marks))
    return ", ".join(out)


def describe(row: dict, settings: dict, age: str = "", look: str = "") -> Tuple[str, str]:
    """(subject, character) from a Portrait Scout row: gender, race and the animation name (which says what the
    speaker wears: 'Cleric Female Elf', 'Peasant Woman', 'Fat Man'). Nothing here is certain - it is a starting
    point the user can edit before generating."""
    race = str(row.get("race") or "").strip()
    anim = str(row.get("anim") or "").strip()
    sex = str(row.get("sex") or "").strip().lower()
    aw = set(re.findall(r"[a-z]+", anim.lower()))
    gender = "female" if sex == "female" or aw & {"female", "woman", "girl"} else \
             "male" if sex == "male" or aw & {"male", "man", "boy"} else ""
    races = {k: v for k, v in settings.get("race_terms", {}).items() if not k.startswith("_")}
    rkey = race.lower().replace("-", " ").replace("_", " ")
    rkey = re.sub(r"^half ?(orc|elf|ogre)$", r"half \1", rkey)      # RACE.IDS writes HALFORC, HALFELF
    _SPRITE_RACES = ("half elf", "half orc", "elf", "dwarf", "halfling", "gnome", "human")
    anim_race = next((k for k in _SPRITE_RACES if set(k.split()) <= aw), "")
    if not rkey or rkey.startswith("race "):                         # no usable race: look in the animation name
        rkey = anim_race
    elif rkey == "human" and anim_race and anim_race != "human":     # human is the race byte's default, so a sprite
        rkey = anim_race                                             # picked on purpose is the better evidence.
    kind = next((k for k in races if k and k not in _SPRITE_RACES and set(k.split()) <= aw), "")
    if kind and (rkey not in races or rkey in _VAGUE_RACES):         # a sirine's file says FAIRY and only its
        rkey = kind                                                  # sprite says sirine; the sprite wins there
    said_kind = str((row.get("colors") or {}).get("kind") or "")    # DrowSkin and the like: the surest of all
    if said_kind in races and said_kind != rkey:
        rkey = said_kind
    named = set(re.findall(r"[a-z]+", str(row.get("name") or "").lower()))
    kind = next((k for k in sorted((x for x in races if x and set(x.split()) <= named),
                                   key=lambda x: -len(x.split())) if k not in _NAME_BLIND), "")
    if (kind and kind != rkey and (rkey in _VAGUE_RACES or rkey in _SPRITE_RACES)
            and not (rkey.startswith("half ") and kind == rkey.split()[-1])):   # a half-elf is not an elf
        rkey = kind                                                  # a creature called Drow whose file says ELF
    #                                                                  is a drow: the kinds the race field has no
    #                                                                  room for live in the name instead
    #   Only this way round: a half-elf, tiefling or werewolf has no sprite of its own and borrows one, so a
    #   non-human file race keeps what it says however the creature walks around the map.
    race_txt = races.get(rkey, "")
    if not race_txt and rkey:                                        # a race nobody has written a line for: the
        race_txt = (f"{rkey}, with the head and face of a {rkey} and not of a person"   # model paints a human
                    if rkey not in _HUMANOID else rkey)              # unless it is told plainly not to
    if rkey and rkey not in _HUMANOID and rkey not in races and not gender:
        subject = anim.lower() or rkey                               # a wolf, a golem...: say what it is
        return subject, subject
    age_txt = (settings.get("age_terms") or {}).get(age, age.lower()) if age else \
        ("young" if aw & {"boy", "girl", "child"} else "")       # a grown person is what it paints anyway:
    #                                                            only an age that is not the default is worth saying
    subject = " ".join(x for x in (gender, race_txt.split(",")[0]) if x) or "person"
    lead, _, extra = age_txt.partition(",")                          # "old, lined face" -> "old male human, lined face"
    parts = [" ".join(x for x in (lead.strip(), gender, race_txt) if x)]
    if extra.strip():
        parts.append(extra.strip())
    manlike = rkey in ("", "human", "elf", "half elf", "dwarf", "halfling", "gnome", "half orc", "tiefling",
                       "drow", "aasimar", "duergar", "genasi")
    col = row.get("colors") or {} if settings.get("use_creature_colors", True) else {}
    bald = bool(aw & {str(w).lower() for w in (settings.get("bald_animations") or [])
                      if not str(w).startswith("_")})            # the sprite is bald; the file does not know it
    feat = features(face_key(row), settings, bool(col.get("hair")), bald) if manlike else ""
    #                                                            a neat side parting on a gnoll helps nobody
    hair_said = False
    if col.get("hair"):
        feat = _HAIR_COLOR.sub("", feat)                        # the file says what color it is; the draw only
        at = re.search(r"\bhair\b", feat)                      # says what it does. One clause, not two: "short
        if at and not any(w in feat.lower() for w in _BALD):     # neat hair, silver-white hair" is the same hair
            adj = re.sub(r"\s*hair\s*$", "", str(col["hair"])).strip()
            if adj:
                feat = feat[:at.start()] + adj + " " + feat[at.start():]
                hair_said = True
    said = parts[0].lower()                                      # what the race already covers
    covered = not manlike or any(w in said for w in ("fur", "scale", "hide", "feather", "pelt", "undead",
                                                     "ghostly", "translucent", "skeleton", "bones", "rotting",
                                                     "bandages", "shadow", "mist"))
    #   the color bytes are a sprite's palette: on a face with no skin and no hair - a mind flayer's tentacles,
    #   a doppelganger's blank gray - they are nonsense. What it wears still comes through.
    #                                                            not a face with skin and
    col = {k: v for k, v in col.items() if k != "kind"}          # a label, not something to say
    for slot in ("skin", "hair"):                                # hair on it. Right behind the race: at the end
        if slot == "hair" and hair_said:                         # of a long description the model ignores color
            continue
        if not col.get(slot) or slot in said or covered:
            continue
        if slot == "hair" and (any(w in feat.lower() for w in _BALD)     # no hair color on a bald head, nor
                               or any(w in said for w in _COVERED_HEAD)):   # under a helm the sprite never takes off
            continue
        parts.append(col[slot])
    took_role = any(w in race_txt.lower() for w in ("ghostly", "translucent", "mist", "darkness"))
    #                                                            nothing is wearing armor inside a spirit, and the
    #                                                            sprite's class says only which BAM was borrowed
    bare = {str(x).lower().replace("-", " ") for x in (settings.get("bare_races") or []) if not str(x).startswith("_")}
    for word, phrase in settings.get("animation_terms", {}).items():
        if word.startswith("_") or word not in aw or phrase in parts or rkey in bare:
            continue                                             # a sahuagin's class says which BAM it borrowed,
        #                                                        not that it is wearing a soldier's kit
        if word in _ROLES:                                       # PEASANT_WOMAN_FIGHTER is one person, not two:
            if took_role:                                        # the class is said before the station, so it wins
                continue
            took_role = True
        parts.append(phrase)
    if feat:
        parts.append(feat)
    if col:
        major, minor = col.get("major", ""), col.get("minor", "")
        if major:
            parts.append(f"dressed in {major} with {minor} trim" if minor and minor != major
                         else f"dressed in {major}")
        if col.get("metal") and col["metal"] not in ("dark iron",):
            parts.append(col["metal"] + " fittings")
    look_txt = (settings.get("look_terms") or {}).get(look, "") if look else ""
    if look_txt:
        parts.append(look_txt)
    return subject, ", ".join(p for p in parts if p)


CURRENT_LOOK = "Current look"


def style_names(settings: dict) -> List[str]:
    """The looks the user can choose from. The top-level 'prompt' is only a fallback now, so it is not offered."""
    return [k for k, v in (settings.get("styles") or {}).items() if not k.startswith("_") and isinstance(v, dict)]


def style_text(settings: dict, style: str = "") -> Tuple[str, str]:
    """The prompt template and negative prompt of a style, as they are now - what the user edits."""
    look = (settings.get("styles") or {}).get(style or str(settings.get("style", "")))
    if isinstance(look, dict) and look.get("prompt"):
        return str(look["prompt"]), str(look.get("negative_prompt", settings.get("negative_prompt", "")))
    return str(settings.get("prompt", "")), str(settings.get("negative_prompt", ""))


def style_zoom(settings: dict, style: str = "") -> Optional[float]:
    """How much the .bmp should be cropped in for this look, when the look says so."""
    look = (settings.get("styles") or {}).get(style or str(settings.get("style", "")))
    try:
        return max(1.0, min(2.5, float(look["zoom"]))) if isinstance(look, dict) and look.get("zoom") else None
    except (TypeError, ValueError):
        return None


def baseline_background() -> str:
    return str(DEFAULTS["background"])


def baseline_text(style: str = "") -> Optional[Tuple[str, str]]:
    """What this style said before anyone edited it, or None for a style of the user's own making."""
    look = (DEFAULTS["styles"] or {}).get(style)
    if isinstance(look, dict):
        return str(look["prompt"]), str(look.get("negative_prompt", DEFAULTS["negative_prompt"]))
    if style in ("", CURRENT_LOOK, str(DEFAULTS["style"])):
        return str(DEFAULTS["prompt"]), str(DEFAULTS["negative_prompt"])
    return None


def save_style(settings: dict, style: str, prompt: str, negative: str, zoom: float = 0, background: str = "") -> None:
    """Keep an edited prompt in settings.json (and in the settings already loaded)."""
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
    except (OSError, ValueError) as e:
        raise GeneratorError(f"generator/settings.json could not be read: {e}") from e
    if not isinstance(raw, dict):
        raw = {}
    if background and background != str(settings.get("background", "")):
        raw["background"] = settings["background"] = background      # shared by every look
    if style in ("", CURRENT_LOOK):
        raw["prompt"], raw["negative_prompt"] = prompt, negative
        settings["prompt"], settings["negative_prompt"] = prompt, negative
    else:
        for d in (raw.setdefault("styles", {}), settings.setdefault("styles", {})):
            look = d.get(style)
            new = {**(look if isinstance(look, dict) else {}), "prompt": prompt, "negative_prompt": negative}
            if zoom:
                new["zoom"] = round(float(zoom), 2)
            if negative == str(settings.get("negative_prompt", "")):  # the shared one: let the style keep following it
                new.pop("negative_prompt", None)
            d[style] = new
    try:
        SETTINGS_FILE.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    except OSError as e:
        raise GeneratorError(f"generator/settings.json could not be written: {e}") from e


def save_patch(settings: dict, patch: dict) -> None:
    """Change a few settings (engine address, picture size, steps...) in settings.json and in the loaded settings."""
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
    except (OSError, ValueError) as e:
        raise GeneratorError(f"generator/settings.json could not be read: {e}") from e
    if not isinstance(raw, dict):
        raw = {}
    try:
        SETTINGS_FILE.write_text(json.dumps(_merge(raw, patch), indent=2), encoding="utf-8")
    except OSError as e:
        raise GeneratorError(f"generator/settings.json could not be written: {e}") from e
    settings.update(_merge(settings, patch))


def pick_palette(settings: dict, text: str) -> str:
    """{random} in the background text -> one of settings['background_palettes'], chosen fresh each time."""
    if "{random}" not in text:
        return text
    choices = [p for p in (settings.get("background_palettes") or []) if str(p).strip()]
    return text.replace("{random}", random.choice(choices) if choices else "muted color")


def fill_prompt(template: str, subject: str, character: str, extra: str = "", background: str = "") -> str:
    """A prompt template with the creature's description (and the background) put in. An empty background takes the
    whole sentence about it out, rather than leaving 'Behind the head, .' in the prompt."""
    character = ", ".join(x.strip().strip(",") for x in (character, extra) if x and x.strip())
    if not background.strip():
        parts = re.split(r"(?<=[.!?])\s+", template)
        template = " ".join(p for p in parts if "{background}" not in p)
    return (template.replace("{subject}", subject.strip()).replace("{character}", character)
            .replace("{background}", background.strip()).strip())


def style_background(settings: dict, style: str = "") -> str:
    """What goes behind the head for this look: the style's own, or the one shared by all of them."""
    look = (settings.get("styles") or {}).get(style or str(settings.get("style", "")))
    if isinstance(look, dict) and look.get("background"):
        return str(look["background"])
    return str(settings.get("background", ""))


def build_prompt(settings: dict, subject: str, character: str, extra: str = "", style: str = "") -> Tuple[str, str]:
    tmpl, negative = style_text(settings, style)
    bg = pick_palette(settings, style_background(settings, style))
    return fill_prompt(tmpl, subject, character, extra, bg), negative


# ------------------------------------------------------------------------------------------- engine
class Engine:
    """An image generator behind the Automatic1111 / Forge web API. Started on demand, stopped at exit."""

    def __init__(self, settings: dict, log: Callable[[str], None] = lambda s: None) -> None:
        self.s = settings
        self.e = settings.get("engine", {})
        self.url = str(self.e.get("url", "http://127.0.0.1:7861")).rstrip("/")
        self.log = log
        # progress(fraction or None, text): fraction 0..1 of the whole job, None while it cannot be measured
        self.progress: Callable[[Optional[float], str], None] = lambda frac, text: None
        self.proc: Optional[subprocess.Popen] = None
        self.canceled = threading.Event()                           # set by cancel(): stop after the current picture
        self.keep = False                                            # True: leave the engine running when the program exits
        atexit.register(lambda: None if self.keep else self.stop())

    def _call(self, path: str, payload: Optional[dict] = None, timeout: float = 10) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return json.loads(body) if body else {}

    def alive(self) -> bool:
        try:
            self._call("/sdapi/v1/samplers", timeout=4)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def start(self) -> None:
        """Make sure the engine answers; start our own copy if allowed."""
        if self.alive():
            return
        cmd = str(self.e.get("start") or "").strip()
        if not self.e.get("autostart", True) or not cmd:
            raise GeneratorError(f"Nothing is answering at {self.url}. No image generator comes with Portrait Scout: "
                                 "start your own with its API on, or put its address in engine.url in "
                                 "generator/settings.json (engine.autostart and engine.start can start it for you).")
        root = HERE.parent
        port = self.url.rsplit(":", 1)[-1].split("/")[0]
        cmd = cmd.replace("{port}", port)
        first = cmd.split()[0]
        if not (root / first).exists() and not Path(first).exists():
            raise GeneratorError(f"The engine start command was not found: {first}")
        if "start_engine.bat" in cmd and not (root / "engine" / "webui").is_dir():
            raise GeneratorError("No image engine is installed. Portrait Scout does not come with one, nor with any "
                                 "models: they are many gigabytes and separately licensed. Put a copy of Forge in the "
                                 "'engine' folder (generator/README.md says how, and which two model files to add), or "
                                 "point engine.url in generator/settings.json at a generator you already run.")
        self.log("Starting the image engine (the first start loads the model and can take several minutes)...")
        show = bool(self.e.get("show_window"))                       # a window of its own, to watch it work
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | (0 if show else
                                                                          getattr(subprocess, "CREATE_NO_WINDOW", 0))
        logf = None if show else open(root / "generator" / "engine.log", "w", encoding="utf-8", errors="replace")
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(root), shell=True, stdout=logf,
                                         stderr=subprocess.STDOUT if logf else None,
                                         stdin=subprocess.DEVNULL, creationflags=flags,
                                         start_new_session=(os.name != "nt"))
        except OSError as e:
            raise GeneratorError(f"The engine could not be started: {e}") from e
        began = time.time()
        deadline = began + float(self.e.get("start_timeout_seconds", 600))
        while time.time() < deadline:
            if self.alive():
                self.log("Image engine is ready.")
                return
            if self.proc.poll() is not None:
                raise GeneratorError("The image engine stopped while starting. See generator/engine.log for why.")
            last = ""
            try:                                                     # what the engine is printing right now
                tail = (root / "generator" / "engine.log").read_text(encoding="utf-8", errors="replace")[-600:]
                last = next((x.strip() for x in reversed(tail.splitlines()) if x.strip()), "")[:90]
            except OSError:
                pass
            self.progress(None, f"Starting the image engine... {int(time.time() - began)} s" + (f"   [{last}]" if last else ""))
            time.sleep(2)
        raise GeneratorError("The image engine did not answer in time. See generator/engine.log.")

    def stop(self, force: bool = False) -> None:
        """Stop the engine if this program started it. force: also one left running by an earlier session
        (asks it to quit over its API; works for engines started with --api-server-stop, as start_engine.bat does)."""
        p, self.proc = self.proc, None
        if p is None or p.poll() is not None:
            if force and self.alive():
                try:
                    self._call("/sdapi/v1/server-kill", {}, timeout=5)
                except urllib.error.HTTPError as e:
                    raise GeneratorError("This engine was not started by Portrait Scout's start_engine.bat (or by an older "
                                         "version of it), so it cannot be stopped from here. Close it yourself this once: "
                                         "end the python.exe that is using the graphics card in Task Manager.") from e
                except (urllib.error.URLError, OSError, ValueError):
                    pass                                             # it quits without answering
                for _ in range(10):
                    if not self.alive():
                        break
                    time.sleep(1)
            return
        try:
            if os.name == "nt":                                      # the .bat starts python: end the whole tree
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                import signal
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except OSError:
            pass

    def cancel(self) -> None:
        """Stop generating without stopping the engine: tell it to drop the picture it is painting, and make the
        loop stop before the next one. Whatever is already finished is kept."""
        self.canceled.set()
        for path in ("/sdapi/v1/interrupt", "/sdapi/v1/skip"):       # Forge drops the picture in hand
            try:
                self._call(path, {}, timeout=5)
            except Exception:  # noqa: BLE001 - it may not be listening; the loop stops either way
                pass

    def generate(self, prompt: str, negative: str, count: int,
                 on_image: Callable[[int, bytes], None] = lambda i, b: None,
                 seed: Optional[int] = None) -> List[bytes]:
        """`count` candidate images (PNG bytes), one request each so they can be shown as they arrive."""
        self.start()
        self.canceled.clear()
        payload = {k: v for k, v in self.s.get("payload", {}).items() if not k.startswith("_")}
        payload.update(prompt=prompt, negative_prompt=negative, n_iter=1)
        base = int(seed) if seed is not None else -1                 # the creature's own seed, so a re-run matches
        out: List[bytes] = []
        count = max(1, int(count))
        for i in range(count):
            if self.canceled.is_set():                              # Stop was pressed: the ones already made stay
                self.log(f"Stopped after {len(out)} of {count}.")
                break
            if base >= 0:
                payload["seed"] = (base + i) % 2147483647
            self.log(f"Generating candidate {i + 1} of {count}...")
            self.progress(i / count, f"Candidate {i + 1} of {count}: starting...")
            stop = threading.Event()

            def watch(i: int = i, stop: threading.Event = stop) -> None:
                began = time.time()
                while not stop.wait(1.0):                            # ask the engine how far it is, once a second
                    try:
                        pr = self._call("/sdapi/v1/progress?skip_current_image=true", timeout=4)
                    except (urllib.error.URLError, OSError, ValueError):
                        continue
                    stt = pr.get("state") or {}
                    step, steps = int(stt.get("sampling_step") or 0), int(stt.get("sampling_steps") or 0)
                    frac = min(1.0, max(0.0, float(pr.get("progress") or 0)))
                    if stop.is_set():
                        break
                    if step or frac:
                        self.progress((i + frac) / count, f"Candidate {i + 1} of {count}: step {step} of {steps}" if steps
                                      else f"Candidate {i + 1} of {count}: {int(frac * 100)}%")
                    else:
                        self.progress(i / count, f"Candidate {i + 1} of {count}: loading the model... {int(time.time() - began)} s")
            threading.Thread(target=watch, daemon=True).start()
            try:
                res = self._call("/sdapi/v1/txt2img", payload, timeout=float(self.e.get("image_timeout_seconds", 900)))
            except urllib.error.HTTPError as e:
                stop.set()
                detail = e.read().decode("utf-8", "replace")[:400]
                raise GeneratorError(f"The engine refused the request ({e.code}): {detail}") from e
            except (urllib.error.URLError, OSError) as e:
                stop.set()
                raise GeneratorError(f"The engine stopped answering: {e}") from e
            stop.set()
            imgs = res.get("images") or []
            if not imgs:
                raise GeneratorError("The engine returned no image.")
            data = base64.b64decode(imgs[0].split(",", 1)[-1])
            out.append(data)
            on_image(i, data)
            self.progress((i + 1) / count, f"Candidate {i + 1} of {count} done")
        return out


_INCORPOREAL = ("ghostly", "translucent", "apparition", "spectre", "specter", "wraith", "ghost",
                "undead spirit", "made of mist", "column of pale gray smoke", "living shadow")


def incorporeal(text: str) -> bool:
    """Whether a description is of something with no solid body. The model paints these as a pale, solid person
    however they are worded, so the fading is done afterwards instead."""
    t = (text or "").lower()
    return any(w in t for w in _INCORPOREAL)


def ghost_image(data: bytes, depth: float = 0.55) -> bytes:
    """Fade a painted figure into its own background, dissolving from the chest up, and leave the eyes burning.
    This is the see-through no prompt gets out of the model: it will not paint a transparent person, so the
    picture is made transparent once it exists. depth 0 returns the picture untouched."""
    if depth <= 0:
        return data
    try:
        import io
        from PIL import Image, ImageChops, ImageFilter
    except ImportError:
        return data                                              # no Pillow: the portrait is simply not faded
    try:
        depth = min(1.5, depth)                                  # past 1 it keeps going: smokier, and higher up
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        over = max(0.0, depth - 1.0)                             # how far past "all of it" the slider has gone
        wash = im.filter(ImageFilter.GaussianBlur(max(8, int(w // 9 * (1 + over)))))   # what is behind her
        base = max(0.0, 1.0 - depth * 0.6)                       # solid at the brow, gone by the bottom - and
        y0, y1 = 0.42 - over * 0.34, 0.97                        # the dissolve starts higher the further it goes

        def fade(v: int) -> int:
            y = v / 255.0
            a = base if y <= y0 else 0.0 if y >= y1 else base * (y1 - y) / (y1 - y0)
            return int(round(a * 255))
        mask = Image.linear_gradient("L").resize((w, h)).point(fade)
        out = Image.composite(im, wash, mask)

        gray = im.convert("L")                                   # the eyes: the brightest spots in the top half
        hist, total, cut = gray.histogram(), w * h, 255
        seen = 0
        for v in range(255, -1, -1):                             # the brightest 2% of the picture
            seen += hist[v]
            if seen >= total * 0.02:
                cut = v
                break
        spots = gray.point(lambda v: 255 if v >= max(cut, 170) else 0)
        top = Image.new("L", (w, h), 0)
        top.paste(255, (0, 0, w, int(h * 0.55)))
        glow = ImageChops.multiply(spots, top).filter(ImageFilter.GaussianBlur(max(1, w // 90)))
        out = Image.composite(im, out, glow)
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
    except (OSError, ValueError):
        return data


SKIN_TINTS = {                                                   # name: (hue in degrees, how much color)
    "Green": (110, 1.30), "Gray": (210, 0.55), "Ashen white": (200, 0.35), "Blue": (220, 1.25),
    "Red-orange": (18, 1.55), "Yellow-brown": (40, 1.30), "Purple-gray": (280, 0.75), "Sea green": (150, 1.20),
}


def suggest_tint(text: str) -> str:
    """Which tint the description is asking for, if any - so the button opens on the right color."""
    t = (text or "").lower()
    for word, name in (("ashen", "Ashen white"), ("charcoal", "Gray"), ("gray skin", "Gray"), ("gray skin", "Gray"),
                       ("green skin", "Green"), ("sea-green", "Sea green"), ("sea green", "Sea green"),
                       ("orange-red skin", "Red-orange"), ("red skin", "Red-orange"), ("blue skin", "Blue"),
                       ("purple-gray", "Purple-gray"), ("yellow-brown", "Yellow-brown"), ("sirine", "Green"),
                       ("orc", "Green"), ("hobgoblin", "Red-orange"), ("drow", "Gray")):
        if word in t:
            return name
    return ""


SPOT_TINTS = {                           # name: (hue in degrees, how colorful to make it, how bright; -1 = leave)
    "Leave": (0, 0, -1), "Amber": (35, 185, -1), "Green": (110, 165, -1), "Blue": (215, 175, -1),
    "Pale blue": (205, 85, -1), "Violet": (280, 160, -1), "Red": (5, 205, -1), "Gold": (45, 195, -1),
    "Copper": (20, 165, -1), "Teal": (175, 160, -1), "Pink": (330, 150, -1), "Silver": (210, 25, 200),
    "Black": (0, 0, 35), "White": (0, 0, 235),
    "Match skin": (-1, 90, -1),              # the face's own hue, read off the picture: for an ear the engine
}                                            # has painted translucent orange, a gem that fights the face...
EYE_TINTS = SPOT_TINTS                   # it began as an eye color; it works on anything you click
GLOW_TINTS = {                           # name: (the hot middle, the light it throws)
    "Fire": ((255, 236, 186), (255, 132, 28)),
    "Hellfire": ((255, 196, 150), (218, 34, 18)),
    "Witchlight": ((214, 255, 212), (58, 224, 92)),
    "Ghost blue": ((214, 246, 255), (64, 170, 255)),
    "Gold": ((255, 246, 198), (236, 176, 36)),
    "Violet": ((238, 212, 255), (158, 70, 240)),
    "Cold white": ((255, 255, 255), (198, 220, 236)),
    "Shadow": ((8, 6, 10), (26, 20, 30)),                        # the other way: eye sockets as pits
}
BLOOD_TINTS = {                          # name: (deep in the cut, raw at its edge, the bruise around it)
    "Blood": ((104, 18, 16), (178, 52, 40), (96, 44, 74)),
    "Dark blood": ((62, 12, 12), (120, 30, 26), (70, 34, 58)),
    "Fresh blood": ((150, 22, 20), (214, 70, 54), (112, 50, 80)),
    "Black ichor": ((22, 18, 24), (58, 50, 62), (40, 36, 52)),
    "Green ichor": ((26, 84, 40), (74, 158, 86), (44, 78, 62)),
    "Blue ichor": ((24, 54, 112), (64, 110, 186), (46, 50, 96)),
    "Gold ichor": ((132, 96, 18), (206, 166, 58), (96, 74, 40)),
}


def _shade(rgb: Tuple[int, int, int], val: float, sat: float, lift: int = 0) -> Tuple[int, int, int]:
    """The same color darker or lighter, and less of it: for a scar cut out of the skin it lies in."""
    import colorsys
    h_, s_, v_ = colorsys.rgb_to_hsv(*(max(0, min(255, c)) / 255 for c in rgb))
    v_ = max(0.0, min(1.0, v_ * val + lift / 255))
    s_ = max(0.0, min(1.0, s_ * sat))
    r_, g_, b_ = colorsys.hsv_to_rgb(h_, s_, v_)
    return int(r_ * 255), int(g_ * 255), int(b_ * 255)
LIP_TINTS = {                                                    # name: (hue in degrees, how much color)
    "Leave": (0, 0.0), "Red": (6, 1.70), "Dark red": (2, 1.35), "Berry": (340, 1.45), "Pink": (352, 1.25),
    "Pale": (20, 0.55), "Gray": (210, 0.45), "Green": (110, 1.30), "Black": (0, 0.20),
}


def stretch_image(data: bytes, points: Optional[List[Tuple[float, float]]] = None, amount: float = 0.0,
                  size: float = 0.12, cells: int = 30) -> bytes:
    """Pull what was clicked away from the middle of the picture, with a taper: an ear drawn out to a point, a
    jaw lengthened, a horn pulled up. Minus pulls it back in, which rounds off an ear that came out too elven.
    The direction is outward from the center of the picture, so the two ears of a face go opposite ways from
    one setting; a mark near the middle is pulled straight up instead."""
    if not points or abs(amount) < 0.01:
        return data
    try:
        import io
        import math
        from PIL import Image
    except ImportError:
        return data
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        gx = max(8, int(cells))
        gy = max(8, int(cells * h / max(1, w)))
        pts = []
        for fx, fy in points[:8]:
            cx, cy = fx * w, fy * h
            ax, ay = cx - w / 2.0, cy - h * 0.42                 # out from the middle of a face, not of a frame
            far = math.hypot(ax, ay)
            if far < w * 0.08:                                   # too near the middle to have an outward: up
                ax, ay = 0.0, -1.0
            else:
                ax, ay = ax / far, ay / far
            r = max(8.0, size * w)
            pts.append((cx, cy, r, max(-1.0, min(1.0, amount)), ax, ay))

        def moved(x: float, y: float) -> Tuple[float, float]:
            for cx, cy, r, st, ax, ay in pts:
                d = math.hypot(x - cx, y - cy)
                if d < r:
                    f = (1.0 - d / r) ** 2 * st * r * 0.45       # strongest under the click, nothing at the edge
                    x, y = x - ax * f, y - ay * f                # paint taken from behind it, so the tip draws out
            return x, y
        mesh = []
        for j in range(gy):
            for i in range(gx):
                x0, y0 = i * w / gx, j * h / gy
                x1, y1 = (i + 1) * w / gx, (j + 1) * h / gy
                nw, sw, se, ne = moved(x0, y0), moved(x0, y1), moved(x1, y1), moved(x1, y0)
                mesh.append(((int(x0), int(y0), int(x1), int(y1)),
                             (nw[0], nw[1], sw[0], sw[1], se[0], se[1], ne[0], ne[1])))
        buf = io.BytesIO()
        im.transform(im.size, Image.MESH, mesh, Image.BICUBIC).save(buf, "PNG")
        return buf.getvalue()
    except (OSError, ValueError):
        return data


def flip_image(data: bytes) -> bytes:
    """The picture mirrored left to right. The engine favors one three-quarter turn of the head, so a crowd of
    them all face the same way; flipping some of them breaks that up."""
    try:
        import io
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO()
        ImageOps.mirror(im).save(buf, "PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - no Pillow, or a picture it will not open
        return data


def warp_image(data: bytes, points: Optional[List[Tuple[float, float]]] = None, amount: float = 0.0,
               size: float = 0.17, cells: int = 30) -> bytes:
    """Push a face out of shape where it was clicked: + swells, - pinches. The engine paints everybody
    good-looking and no wording moves it - a broken nose comes out straight - so the shape is changed in the
    picture afterwards. Done by bending a grid over the picture, which keeps the paint intact."""
    if not points or abs(amount) < 0.01:
        return data
    try:
        import io
        import math
        from PIL import Image
    except ImportError:
        return data
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        gx = max(8, int(cells))
        gy = max(8, int(cells * h / max(1, w)))
        pts = [(fx * w, fy * h, max(8.0, size * w), max(-1.0, min(1.0, amount))) for fx, fy in points[:8]]

        def moved(x: float, y: float) -> Tuple[float, float]:
            for cx, cy, r, s in pts:
                dx, dy = x - cx, y - cy
                d = math.hypot(dx, dy)
                if 0.001 < d < r:
                    f = (1.0 - d / r) ** 2 * s                   # strongest under the click, nothing at the edge
                    x, y = x - dx * f, y - dy * f                # taking paint from nearer the middle swells it
            return x, y
        mesh = []
        for j in range(gy):
            for i in range(gx):
                x0, y0 = i * w / gx, j * h / gy
                x1, y1 = (i + 1) * w / gx, (j + 1) * h / gy
                nw, sw, se, ne = moved(x0, y0), moved(x0, y1), moved(x1, y1), moved(x1, y0)
                mesh.append(((int(x0), int(y0), int(x1), int(y1)),
                             (nw[0], nw[1], sw[0], sw[1], se[0], se[1], ne[0], ne[1])))
        buf = io.BytesIO()
        im.transform(im.size, Image.MESH, mesh, Image.BICUBIC).save(buf, "PNG")
        return buf.getvalue()
    except (OSError, ValueError):
        return data


def touch_up(data: bytes, hue: float = 110, amount: float = 0.0, sat: float = 1.3, catch: float = 0.6,
             light: float = 0.0, lip_hue: float = 6, lip_amount: float = 0.0, lip_sat: float = 1.7,
             spots: Optional[List[Tuple[float, float]]] = None, eye_hue: float = 35, eye_amount: float = 0.0,
             eye_sat: float = 185, eye_size: float = 0.035, eye_val: float = -1, grime: float = 0.0,
             creases: float = 0.0, scars: float = 0.0,
             scar_points: Optional[List[Tuple[float, float]]] = None, wounds: float = 0.0,
             reach: float = 0.17, ridge: float = 0.5, blood: str = "Blood", line: float = 1.0,
             angle: Optional[float] = None, edge: float = 0.5,
             speckles: float = 0.0, speckle_points: Optional[List[Tuple[float, float]]] = None,
             speckle_size: float = 0.16,
             glow: str = "None", glow_tint: str = "Fire", glow_amount: float = 0.0,
             glow_size: float = 0.035, glow_spill: float = 0.5, glow_round: float = 0.42) -> bytes:
    """Work on the skin of a finished picture: its color, how dark it is, and the lips on their own.

    The engine paints a person it recognizes whatever the description says, so this is done afterwards. Skin is
    not found by looking for flesh colors - a picture can have a color cast of its own, and a creature's skin
    may be green or gray already. The face's own hue is read off the picture first (the middle hue inside the
    oval a face sits in, ignoring the dark and the strongly colored), and pixels near that hue are worked on,
    along with the warm band that lips, nose and ears sit in. catch widens both. light makes the skin darker or
    lighter, from -1 to 1. The lips are the warm, colored part of that, and can be given a color of their own.
    """
    lights = spots if (glow in ("Glow", "Flame") and glow_amount > 0) else None
    if (amount <= 0 and lip_amount <= 0 and abs(light) < 0.01 and grime <= 0 and creases <= 0 and speckles <= 0
            and not (scar_points and (scars > 0 or wounds > 0)) and not (spots and eye_amount > 0)
            and not lights):
        return data
    try:
        import io
        from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
    except ImportError:
        return data
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        hc, sc, vc = im.convert("HSV").split()
        catch = max(0.0, min(1.0, catch))
        oval = Image.new("L", im.size, 0)                        # skin is where a face is
        ImageDraw.Draw(oval).ellipse((w * 0.04, -h * 0.05, w * 0.96, h * 0.80), fill=255)
        oval = oval.filter(ImageFilter.GaussianBlur(max(1, w // 14)))
        lit = vc.point(lambda x: 255 if 60 <= x <= 250 else 0)   # not hair, not the shadows
        # A fixed ceiling says "anything this colorful is gold or a gem, not skin" - which is true of a human
        # and false of a gith, whose cheek is more saturated than most jewelry. So the face is asked what it is:
        # the middle of the picture is sampled and the ceiling is lifted to clear it.
        mid_box = im.crop((int(w * 0.42), int(h * 0.38), int(w * 0.58), int(h * 0.50))).convert("HSV")
        seen = list(mid_box.getdata())
        face_hue = sorted(q[0] for q in seen)[len(seen) // 2] if seen else 0
        face_sat = sorted(q[1] for q in seen)[len(seen) // 2] if seen else 0
        top_sat = int(max(90 + catch * 165, face_sat + 40 + catch * 60))
        cal = sc.point(lambda x: 255 if 10 <= x <= top_sat else 0)   # not gold, not gems
        here = ImageChops.multiply(ImageChops.multiply(oval.point(lambda x: 255 if x > 128 else 0), lit), cal)
        hist, run, mid = hc.histogram(here), 0, 0
        total = sum(hist) or 1
        for i, n in enumerate(hist):                             # the face's own hue, whatever color it is
            run += n
            if run >= total * 0.5:
                mid = i
                break
        win = int(10 + catch * 26)
        near = hc.point(lambda x: 255 if abs(((x - mid + 128) % 256) - 128) <= win else 0)
        warm = hc.point(lambda x: 255 if x <= int(26 + catch * 18) else 0)         # lips, nose, ears
        face = ImageChops.multiply(ImageChops.multiply(ImageChops.lighter(near, warm), cal),
                                   ImageChops.multiply(lit, oval))
        face = face.filter(ImageFilter.GaussianBlur(max(1, w // 160)))   # feathered, or the patches it does not
        #                                                                  take show up as hard-edged blotches
        hc2, sc2, vc2 = hc, sc, vc
        if amount > 0:
            tgt = int(round(hue % 360 / 360 * 255))
            hc2 = Image.composite(hc.point(lambda x: int(round(x + (tgt - x) * min(1.0, amount)))), hc, face)
            sc2 = Image.composite(sc.point(lambda x: max(0, min(255, int(round(x * sat))))), sc, face)
        if abs(light) >= 0.01:                                   # darker or lighter skin, the rest left alone
            k = 1.0 - max(-1.0, min(1.0, light)) * 0.55          # + is darker: the slider is called Darker
            vc2 = Image.composite(vc.point(lambda x: max(0, min(255, int(round(x * k))))), vc, face)
        if lip_amount > 0:                                       # the lips: the colored part of the lower face,
            mouth = Image.new("L", im.size, 0)                   # and not the nose, the ears or a red gem
            ImageDraw.Draw(mouth).ellipse((w * 0.26, h * 0.46, w * 0.74, h * 0.74), fill=255)
            mouth = mouth.filter(ImageFilter.GaussianBlur(max(1, w // 40)))
            lips = ImageChops.multiply(ImageChops.multiply(warm, sc.point(lambda x: 255 if x >= 70 else 0)),
                                       ImageChops.multiply(lit, mouth))
            ltgt = int(round(lip_hue % 360 / 360 * 255))
            hc2 = Image.composite(hc.point(lambda x: int(round(x + (ltgt - x) * min(1.0, lip_amount)))), hc2, lips)
            sc2 = Image.composite(sc.point(lambda x: max(0, min(255, int(round(x * lip_sat))))), sc2, lips)
        if spots and eye_amount > 0:                             # a spot has to be pointed at: an iris is no
            rad = max(2.0, eye_size * w)                         # different in color from a cheek. Inside the
            etgt = mid if eye_hue < 0 else int(round(eye_hue % 360 / 360 * 255))   # ring, though, the iris can be
            #                                                    found: read what; a negative hue means the face's own
            from PIL import ImageOps
            bump = ImageOps.invert(Image.radial_gradient("L"))       # full at the click, fading to nothing at the
            bump = bump.point(lambda x: min(255, int(x * 1.7)))      # edge: what is under the finger counts most
            for fx, fy in spots[:8]:
                ring = Image.new("L", im.size, 0)
                cx, cy = fx * w, fy * h
                box = (int(cx - rad), int(cy - rad * 0.85), int(cx + rad), int(cy + rad * 0.85))
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                ring.paste(bump.resize((box[2] - box[0], box[3] - box[1])), box)
                # What is under the click is the iris: take its color and brightness from a few pixels at the
                # center, then keep what matches inside the ring. Lashes are far darker, the white far brighter
                # and the lid a different color, so all three drop out without having to be described.
                px = max(1, int(rad * 0.22))
                patch = im.crop((int(cx - px), int(cy - px), int(cx + px + 1), int(cy + px + 1))).convert("HSV")
                hs, ss, vs = (sum(c[i] for c in patch.getdata()) / max(1, len(patch.getdata())) for i in range(3))
                near_v = vc.point(lambda x: 255 if abs(x - vs) <= 62 else 0)
                near_h = hc.point(lambda x: 255 if abs(((x - hs + 128) % 256) - 128) <= 40 else 0)
                gray_ = sc.point(lambda x: 255 if x < 36 else 0)     # a gray iris has no hue worth matching
                iris = ImageChops.multiply(ImageChops.multiply(ring, near_v),
                                           ImageChops.lighter(near_h, gray_ if ss < 60 else gray_.point(lambda x: 0)))
                iris = iris.filter(ImageFilter.GaussianBlur(max(1, int(rad * 0.12))))   # no hard speckled edges
                a_e = min(1.0, eye_amount)
                if eye_sat > 0:                                  # move the color there, do not scale what is
                    hc2 = Image.composite(hc.point(lambda x: int(round(x + (etgt - x) * a_e))), hc2, iris)
                sc2 = Image.composite(sc.point(lambda x: max(0, min(255, int(round(x + (eye_sat - x) * a_e))))),
                                      sc2, iris)                 # there: a gray iris has nothing to scale up
                if eye_val >= 0:
                    vc2 = Image.composite(vc.point(lambda x: max(0, min(255, int(round(x + (eye_val - x) * a_e))))),
                                          vc2, iris)
                elif eye_sat > 0:                                # a hue on a near-black iris is still near-black:
                    floor = 70 + a_e * 55                        # lift it enough for the color to be seen
                    vc2 = Image.composite(vc.point(lambda x: int(x + (floor - x) * a_e * 0.75) if x < floor else x),
                                          vc2, iris)
        out = Image.merge("HSV", (hc2, sc2, vc2)).convert("RGB")
        if creases > 0:
            # Lines where a face creases. The engine paints an unlined face whatever the description says, and
            # these portraits are framed alike - head filling the frame, chin level - so the brow, the folds
            # beside the nose and the jowls are where they always are. The eyes are taken from the marks when
            # two were placed, for the creases at their outer corners.
            import math
            import random
            a_c = min(1.0, creases)
            lay = Image.new("L", im.size, 0)
            dr = ImageDraw.Draw(lay)
            rnd = random.Random(w * 7919 + h)
            lw = max(1, int(w / 260))
            for i in range(3 + int(4 * a_c)):                    # the brow, each line its own length and none
                inset = 0.33 + rnd.uniform(0, 0.06) + i * 0.012  # of them reaching the sides of the face
                y = h * (0.205 + 0.034 * i) + rnd.uniform(-3, 3)
                dr.arc([w * inset, y - h * 0.04, w * (1 - inset), y + h * 0.04], 205, 335, fill=255, width=lw)
            for ex, ey in (spots[:2] if spots and len(spots) == 2 else ((0.37, 0.40), (0.63, 0.40))):
                for _k in range(2 + int(3 * a_c)):               # the corners of the eyes
                    ang = math.radians(rnd.uniform(-30, 30))
                    x0 = w * ex + (w * 0.05 if ex > 0.5 else -w * 0.05)
                    y0 = h * ey + rnd.uniform(-5, 5)
                    ln = w * rnd.uniform(0.03, 0.06)
                    dr.line([x0, y0, x0 + ln * math.cos(ang) * (1 if ex > 0.5 else -1), y0 + ln * math.sin(ang)],
                            fill=220, width=lw)
            for side in (-1, 1):                                 # the folds beside the nose
                dr.arc([w * (0.5 + side * 0.16) - w * 0.085, h * 0.49,
                        w * (0.5 + side * 0.16) + w * 0.085, h * 0.71],
                       (250 if side < 0 else 290), (290 if side < 0 else 330), fill=255, width=lw + 1)
            lay = lay.filter(ImageFilter.GaussianBlur(max(1, int(w / 700))))
            out = Image.composite(out.point(lambda x: int(x * 0.40)), out, lay.point(lambda x: min(255, int(x * a_c))))
        if scars > 0 and scar_points:
            # A scar is painted, not shaded: darkening what is already there only shows on pale skin, and
            # these portraits are drow, dragons and green hags as often as farmhands. So the skin's own color
            # is read where the scar goes, and the groove, the healed ridge and the light it catches are mixed
            # from it. The swell under the scar comes from warp_image, called before this.
            import math as _m
            import random as _r
            a_s = min(1.0, scars)
            rs = _r.Random(w * 104729 + h)
            far = min(0.40, max(0.05, reach))
            lw_s = max(1, round(w / 200 * (0.6 + far * 2.4) * max(0.4, line)))   # a longer scar is heavier too
            b_g, b_p = max(1, int(w / 900)), max(1, int(w / 700))
            lit, rim_s = max(0.0, min(1.0, ridge)), max(0.0, min(1.0, edge))
            grow = 5 if lw_s > 1 else 3                          # how far the dark line reaches outside the groove
            for fx, fy in scar_points[:8]:
                cx, cy = fx * w, fy * h
                ang = _m.radians(angle + rs.uniform(-8, 8) if angle is not None else   # a set angle, give or take
                                 (rs.uniform(-80, -35) if rs.random() < 0.7 else rs.uniform(35, 80)))
                half = w * far * rs.uniform(0.30, 0.50)          # Reach is how long it runs
                dx, dy = _m.cos(ang) * half, _m.sin(ang) * half
                near = max(2, lw_s * 2)                          # the skin right where it was clicked, not the
                x0, y0 = int(max(0, min(cx - near, w - 2))), int(max(0, min(cy - near, h - 2)))   # whole sweep of
                x1, y1 = int(min(w, max(x0 + 1, cx + near))), int(min(h, max(y0 + 1, cy + near)))  # the line, which
                skin = out.crop((x0, y0, x1, y1)).resize((1, 1), Image.BOX).getpixel((0, 0))       # can run off a face
                groove, heal = _shade(skin, 0.42, 0.75), _shade(skin, 1.14, 0.55, 18)
                shine = _shade(skin, 1.45, 0.30, 46)
                ink, pale, lip = (Image.new("L", im.size, 0) for _ in range(3))
                di, dp, dl = ImageDraw.Draw(ink), ImageDraw.Draw(pale), ImageDraw.Draw(lip)
                di.line([cx - dx, cy - dy, cx + dx, cy + dy], fill=255, width=lw_s + 1)
                dp.line([cx - dx + lw_s, cy - dy, cx + dx + lw_s, cy + dy], fill=255, width=lw_s)
                dl.line([cx - dx - lw_s * 0.8, cy - dy - lw_s * 0.6,  # the lit edge, on the far side of the groove
                         cx + dx - lw_s * 0.8, cy + dy - lw_s * 0.6], fill=255, width=max(1, lw_s - 1))
                ink = ink.filter(ImageFilter.GaussianBlur(b_g))
                pale = pale.filter(ImageFilter.GaussianBlur(b_p))
                lip = lip.filter(ImageFilter.GaussianBlur(max(1, int(w / 1400))))
                out = Image.composite(Image.new("RGB", im.size, groove), out, ink.point(lambda x: int(x * a_s)))
                out = Image.composite(Image.new("RGB", im.size, heal), out, pale.point(lambda x: int(x * a_s * 0.8)))
                if rim_s > 0:                                    # the dark line the shipped portraits draw round
                    ring = ImageChops.subtract(ink.filter(ImageFilter.MaxFilter(grow)), ink).filter(
                        ImageFilter.GaussianBlur(1))             # a feature, so it sits in the face, not on it
                    out = Image.composite(Image.new("RGB", im.size, _shade(skin, 0.22, 0.55)), out,
                                          ring.point(lambda x: int(x * a_s * rim_s * 0.55)))
                if lit > 0:
                    out = Image.composite(Image.new("RGB", im.size, shine), out,
                                          lip.point(lambda x: int(x * a_s * lit)))
        if wounds > 0 and scar_points:
            # A wound is not a scar: raw and red rather than pale and healed, with the skin around it bruised.
            import math as _mw
            import random as _rw
            a_w = min(1.0, wounds)
            rw = _rw.Random(w * 15485863 + h)
            bruise, cut, wet = (Image.new("L", im.size, 0) for _ in range(3))
            db, dc, dw = (ImageDraw.Draw(x) for x in (bruise, cut, wet))
            far = min(0.40, max(0.05, reach))                    # Reach is how long the cut runs
            lw_w = max(1, round(w / 170 * (0.6 + far * 2.4) * max(0.4, line)))
            for fx, fy in scar_points[:8]:
                cx, cy = fx * w, fy * h
                rr = w * far * 0.5
                db.ellipse([cx - rr, cy - rr * 0.8, cx + rr, cy + rr * 0.8], fill=255)
                ang = _mw.radians(angle + rw.uniform(-8, 8) if angle is not None else
                                  (rw.uniform(-75, -30) if rw.random() < 0.6 else rw.uniform(30, 75)))
                half = w * far * rw.uniform(0.24, 0.44)
                dx, dy = _mw.cos(ang) * half, _mw.sin(ang) * half
                dc.line([cx - dx, cy - dy, cx + dx, cy + dy], fill=255, width=lw_w + 1)
                dw.line([cx - dx * 0.8, cy - dy * 0.8 + lw_w, cx + dx * 0.8, cy + dy * 0.8 + lw_w],
                        fill=255, width=max(1, lw_w - 1))
                for _d in range(1 + int(3 * a_w)):               # a little of it has run
                    sx = cx + rw.uniform(-half * 0.7, half * 0.7)
                    sy = cy + rw.uniform(-half * 0.3, half * 0.3)
                    dc.line([sx, sy, sx + rw.uniform(-2, 2), sy + w * far * rw.uniform(0.12, 0.32)],
                            fill=200, width=max(1, lw_w - 1))
            rim = ImageChops.subtract(cut.filter(ImageFilter.MaxFilter(5 if lw_w > 1 else 3)), cut)   # just outside
            rim = ImageChops.lighter(rim, ImageChops.subtract(wet.filter(ImageFilter.MaxFilter(3)), wet))
            bruise = bruise.filter(ImageFilter.GaussianBlur(max(2, int(w / 60))))
            cut = cut.filter(ImageFilter.GaussianBlur(max(1, int(w / 900))))
            wet = wet.filter(ImageFilter.GaussianBlur(max(1, int(w / 700))))
            deep, raw, bruised = BLOOD_TINTS.get(blood) or BLOOD_TINTS["Blood"]
            out = Image.composite(Image.new("RGB", im.size, bruised), out,           # the bruise around it
                                  bruise.point(lambda x: int(x * a_w * 0.55)))
            out = Image.composite(Image.new("RGB", im.size, deep), out,              # the cut itself
                                  cut.point(lambda x: int(x * a_w)))
            out = Image.composite(Image.new("RGB", im.size, raw), out,               # raw at its edge
                                  wet.point(lambda x: int(x * a_w * 0.75)))
            if edge > 0:                                                             # drawn round, like the shipped
                rim_w = max(0.0, min(1.0, edge))                                     # portraits draw every feature
                rim = rim.filter(ImageFilter.GaussianBlur(1))
                out = Image.composite(Image.new("RGB", im.size, _shade(deep, 0.26, 0.70)), out,
                                      rim.point(lambda x: int(x * a_w * rim_w * 0.95)))
            if ridge > 0:                                                            # wet, so it catches the light
                lit_w = max(0.0, min(1.0, ridge))
                out = Image.composite(Image.new("RGB", im.size, _shade(raw, 1.5, 0.35, 60)), out,
                                      wet.point(lambda x: int(x * a_w * lit_w * 0.45)))
        if speckles > 0:
            # Freckles, a gith's markings, the spatter on a pox-scarred face. The engine paints none of them:
            # at this size a freckle is two pixels, and two pixels never survive the model's idea of a face.
            import random as _rp
            a_p = min(1.0, speckles)
            rp = _rp.Random(w * 2654435761 + h * 97)
            # A speckle only has to land on skin, not be recolored, so the hue read off the middle of the face
            # is used directly and the saturation left alone entirely.
            mh = face_hue
            skin_here = ImageChops.multiply(
                ImageChops.multiply(oval.point(lambda x: 255 if x > 110 else 0),
                                    vc.point(lambda x: 255 if 45 <= x <= 252 else 0)),
                hc.point(lambda x: 255 if abs(((x - mh + 128) % 256) - 128) <= 34 else 0))
            # where the face actually is, rather than where the frame says: a generated picture is often taller
            # than the portrait cut out of it, and the forehead is not where freckles go
            bb = skin_here.getbbox() or (0, 0, w, h)
            bw, bh = max(1, bb[2] - bb[0]), max(1, bb[3] - bb[1])

            def spot(rx: float, ry: float, rr: float) -> Tuple[float, float, float]:
                return (bb[0] + rx * bw) / w, (bb[1] + ry * bh) / h, rr * bw / w
            spread = max(0.03, min(0.45, speckle_size))
            where = [(fx, fy, spread) for fx, fy in (speckle_points or [])[:8]] or \
                    [spot(0.50, 0.56, 0.20), spot(0.30, 0.60, 0.28), spot(0.70, 0.60, 0.28)]   # nose, both cheeks
            dots = Image.new("L", im.size, 0)
            dd = ImageDraw.Draw(dots)
            for _k in range(int(30 + 260 * a_p)):
                fx, fy, rr = where[rp.randrange(len(where))]
                ox, oy = rp.gauss(0, rr * 0.5) * w, rp.gauss(0, rr * 0.42) * w
                cx, cy = fx * w + ox, fy * h + oy
                r_ = max(0.6, w * rp.uniform(0.0030, 0.0080))
                dd.ellipse([cx - r_, cy - r_ * 0.9, cx + r_, cy + r_ * 0.9], fill=rp.randrange(150, 256))
            dots = ImageChops.multiply(dots, skin_here)          # on skin, not on hair, cloth or the background
            dots = dots.filter(ImageFilter.GaussianBlur(max(0.6, w / 900)))
            # darker than whatever they fall on, rather than one flat color: a freckle in shadow is still a
            # freckle, and the paint underneath keeps its texture
            darker = Image.merge("RGB", [c.point(lambda x: int(x * 0.68)) for c in out.split()])
            out = Image.composite(darker, out, dots.point(lambda x: int(x * a_p * 0.85)))
        if grime > 0:
            # The engine washes everybody: a peasant comes out as clean as a prince, and no wording stops it.
            # Dirt is a thing done to a picture - soft smudges where sweat and soot gather, and a little grain
            # over the whole of it to break up skin the engine has airbrushed.
            a_g = min(1.0, grime)
            big = Image.effect_noise((max(8, w // 14), max(8, h // 14)), 90).resize((w, h), Image.BICUBIC)
            mid = Image.effect_noise((max(8, w // 5), max(8, h // 5)), 70).resize((w, h), Image.BICUBIC)
            blot = ImageChops.multiply(big, mid).filter(ImageFilter.GaussianBlur(max(1, w // 120)))
            smear = blot.point(lambda x: max(0, min(255, int((x - 96) * 2.4 * a_g))))
            out = Image.composite(Image.new("RGB", im.size, (74, 58, 40)), out, smear)
            grain = Image.effect_noise(im.size, 13 * a_g)
            out = Image.blend(out, ImageChops.overlay(out, Image.merge("RGB", (grain, grain, grain))), 0.28 * a_g)
        if lights:
            # Light, last of all, because light falls on whatever is already there. A glow that is only a bright
            # disc looks stuck on; what sells it is the spill - a wide, faint wash of the same color over the
            # brow and the cheekbone - and a little darkness just outside it to push against.
            import math as _ml
            import random as _rl
            a_l = min(1.0, glow_amount)
            hot, thrown = GLOW_TINTS.get(glow_tint) or GLOW_TINTS["Fire"]
            dim = glow_tint == "Shadow"                          # the same shapes, taken away instead of added
            r0 = max(1.5, w * max(0.005, glow_size))
            far = r0 * (3.0 + 9.0 * max(0.0, min(1.0, glow_spill)))
            seed = _rl.Random(w * 32452843 + h)
            fall = ImageOps.invert(Image.radial_gradient("L"))   # white in the middle, black at the rim

            squash = max(0.15, min(1.0, glow_round))             # 1 is a circle; less is an eye - wide and pointed
            wide_r = r0 / max(0.45, squash) ** 0.5               # a squashed light keeps about the same area

            def _blob(rad: float, gamma: float, eye: bool = True, soft: float = 0.16) -> Image.Image:
                dw_ = max(2, int(rad * 2))
                dh_ = max(2, int(rad * 2 * (squash if eye else 1.0)))
                g = fall.resize((dw_, dh_), Image.BILINEAR)
                if gamma != 1.0:
                    g = g.point(lambda x: int(255 * (x / 255.0) ** gamma))
                if eye and squash < 0.95:                        # an almond, pointed at the corners like an eye
                    cut_m = Image.new("L", (dw_, dh_), 0)
                    a_, b_ = dw_ / 2.0, dh_ / 2.0
                    up, dn = [], []
                    for i in range(25):
                        t = -1.0 + i / 12.0
                        bulge = b_ * (1.0 - t * t) ** 0.65
                        up.append((a_ + t * a_, b_ - bulge))
                        dn.append((a_ + t * a_, b_ + bulge))
                    ImageDraw.Draw(cut_m).polygon(up + dn[::-1], fill=255)
                    g = ImageChops.multiply(g, cut_m.filter(ImageFilter.GaussianBlur(max(1, dh_ * 0.10))))
                m = Image.new("L", im.size, 0)
                for fx, fy in lights[:8]:
                    one = Image.new("L", im.size, 0)
                    one.paste(g, (int(fx * w - dw_ / 2), int(fy * h - dh_ / 2)))
                    m = ImageChops.lighter(m, one)
                return m.filter(ImageFilter.GaussianBlur(max(0.8, rad * soft)))

            def _tongues() -> Image.Image:
                m = Image.new("L", im.size, 0)
                dd = ImageDraw.Draw(m)
                for fx, fy in lights[:8]:
                    cx, cy = fx * w, fy * h
                    for _k in range(5):
                        tall = r0 * seed.uniform(2.2, 4.8)
                        lean = _ml.radians(seed.uniform(-26, 26))
                        wide = r0 * seed.uniform(0.40, 0.85)
                        off = wide_r * seed.uniform(-1.0, 1.0)   # along the whole eye, not from one spot
                        bx, by = cx + off, cy + r0 * 0.35
                        tx, ty = bx + _ml.sin(lean) * tall, by - _ml.cos(lean) * tall
                        mx, my = (bx + tx) / 2, (by + ty) / 2    # a leaf, not a triangle: it bellies out
                        dd.polygon([(bx - wide * 0.7, by), (mx - wide, my), (tx, ty),
                                    (mx + wide, my), (bx + wide * 0.7, by)], fill=255)
                m = m.filter(ImageFilter.GaussianBlur(max(1.2, r0 * 0.55)))
                flick = Image.effect_noise((w, h), 42).filter(ImageFilter.GaussianBlur(max(2, int(r0 * 1.2))))
                return ImageChops.multiply(m, flick.point(lambda x: min(255, 128 + x)))

            core = _blob(wide_r, 0.45, soft=0.06)                # the eye aperture itself: small, hot, and sharp
            lamp = _blob(wide_r * 1.6, 1.6, soft=0.14)           # the light just around it, falling off fast
            halo = ImageChops.lighter(lamp, _tongues()) if glow == "Flame" else lamp
            spill = _blob(far, 2.4, eye=False, soft=0.22)        # light falls on the face every way at once
            if not dim:                                          # a rim of shadow, so the light has something to
                edge_l = ImageChops.subtract(_blob(far * 0.5, 2.0, eye=False, soft=0.2), halo)   # push against
                out = Image.composite(Image.new("RGB", im.size, (24, 20, 26)), out,
                                      edge_l.point(lambda x: int(x * a_l * 0.30)))
            if not dim:                                      # an eye keeps its dark middle, or it is only a bead
                pupil = im.convert("L").point(lambda x: 255 if x < 55 else 0)   # (a shadow wants that middle dark,
                core = ImageChops.subtract(core, pupil.filter(ImageFilter.GaussianBlur(1)).point(
                    lambda x: int(x * 0.55)))                    # so it keeps its core, but a hot eye burns through)
            if dim:
                for shape, col, k in ((spill, thrown, 0.35), (halo, thrown, 0.80), (core, hot, 0.95)):
                    out = Image.composite(Image.new("RGB", im.size, col), out,
                                          shape.point(lambda x: int(x * a_l * k)))
            else:
                black = Image.new("RGB", im.size, (0, 0, 0))
                for shape, col, k in ((spill, thrown, 0.30), (halo, thrown, 0.80)):
                    lay = Image.composite(Image.new("RGB", im.size, col), black,
                                          shape.point(lambda x: int(x * a_l * k)))
                    out = ImageChops.screen(out, lay)
                out = Image.composite(Image.new("RGB", im.size, hot), out,   # the aperture is painted, not screened:
                                      core.point(lambda x: int(x * a_l)))    # screening warm on warm only yellows it
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
    except (OSError, ValueError):
        return data


def skin_tint(data: bytes, hue: float = 110, amount: float = 0.9, sat: float = 1.3, catch: float = 0.6) -> bytes:
    """Kept for older callers: the skin color part of touch_up()."""
    return touch_up(data, hue, amount, sat, catch)


def save_candidates(images: List[bytes], folder: Path, stem: str, ghost: float = 0.0) -> List[str]:
    if ghost > 0:
        images = [ghost_image(d, ghost) for d in images]
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.#!-]+", "_", stem).strip("_") or "portrait"
    paths = []
    for i, data in enumerate(images, 1):
        p = folder / f"{safe}_{stamp}_{i}.png"
        p.write_bytes(data)
        paths.append(str(p))
    return paths


# ------------------------------------------------------------------------------------------- PNG -> game BMP
def _png_rgb(data: bytes) -> Tuple[int, int, bytearray]:
    """(width, height, RGB bytes) of an 8-bit RGB / RGBA / gray PNG that is not interlaced. No Pillow needed."""
    import struct
    import zlib
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos, idat, w = 8, [], 0
    h = depth = ctype = inter = 0
    while pos + 8 <= len(data):
        n, typ = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            w, h, depth, ctype, _c, _f, inter = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    ch = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype, 0)
    if depth != 8 or not ch or inter:
        raise ValueError("this kind of PNG is not supported without Pillow (pip install pillow)")
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    out = bytearray(w * h * 3)
    prev = bytearray(stride)
    for y in range(h):
        o = y * (stride + 1)
        f = raw[o]
        line = bytearray(raw[o + 1:o + 1 + stride])
        if f == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                line[i] = (line[i] + (((line[i - ch] if i >= ch else 0) + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        row = y * w * 3
        if ch == 3:
            out[row:row + w * 3] = line
        elif ch == 4:
            out[row:row + w * 3:3], out[row + 1:row + w * 3:3], out[row + 2:row + w * 3:3] = line[0::4], line[1::4], line[2::4]
        else:                                                        # gray (+ alpha)
            g = line[0::ch]
            out[row:row + w * 3:3], out[row + 1:row + w * 3:3], out[row + 2:row + w * 3:3] = g, g, g
    return w, h, out


def _crop_box(sw: int, sh: int, width: int, height: int, zoom: float, top: float) -> Tuple[int, int, int, int]:
    """(left, top, crop width, crop height): the portrait's shape, `zoom` times closer, centered left to right.
    With zoom 1 the crop is taken from the middle; closer crops start `top` of the way down (heads sit high)."""
    zoom = max(1.0, float(zoom or 1))
    cw = min(sw, round(sh * width / height))
    ch = min(sh, round(cw * height / width))
    if zoom <= 1.0:
        return (sw - cw) // 2, (sh - ch) // 2, cw, ch
    cw, ch = max(width, round(cw / zoom)), max(height, round(ch / zoom))
    cw, ch = min(cw, sw), min(ch, sh)
    y = min(sh - ch, max(0, round(sh * max(0.0, float(top or 0)))))
    return (sw - cw) // 2, y, cw, ch


crop_box = _crop_box                                                 # public name for the same thing


def png_to_portrait_bmp(png_path: str, bmp_path: str, width: int, height: int, zoom: float = 1.0, top: float = 0.0) -> str:
    """Crop the picture to the portrait's shape (see _crop_box), shrink it to width x height and save it as a
    24-bit .bmp, the format the game uses. Uses Pillow when it is installed (sharper), plain Python otherwise."""
    import struct
    width, height = int(width), int(height)
    try:
        from PIL import Image
        im = Image.open(png_path).convert("RGB")
        sw, sh = im.size
        left, top_, cw, chh = _crop_box(sw, sh, width, height, zoom, top)
        im.crop((left, top_, left + cw, top_ + chh)).resize((width, height), Image.LANCZOS).save(bmp_path, "BMP")
        return bmp_path
    except ImportError:
        pass
    sw, sh, px = _png_rgb(Path(png_path).read_bytes())
    left, top_, cw, chh = _crop_box(sw, sh, width, height, zoom, top)
    xs = [left + cw * i // width for i in range(width + 1)]
    ys = [top_ + chh * j // height for j in range(height + 1)]
    pad = (4 - width * 3 % 4) % 4
    rows = []
    for j in range(height - 1, -1, -1):                              # BMP rows go bottom-up, pixels B G R
        y0, y1 = ys[j], max(ys[j] + 1, ys[j + 1])
        line = bytearray()
        for i in range(width):
            x0, x1 = xs[i], max(xs[i] + 1, xs[i + 1])
            r = g = b = 0
            for yy in range(y0, y1):
                seg = px[(yy * sw + x0) * 3:(yy * sw + x1) * 3]
                r += sum(seg[0::3])
                g += sum(seg[1::3])
                b += sum(seg[2::3])
            cnt = (y1 - y0) * (x1 - x0)
            line += bytes((b // cnt, g // cnt, r // cnt))
        rows.append(bytes(line) + b"\0" * pad)
    body = b"".join(rows)
    head = b"BM" + struct.pack("<IHHI", 54 + len(body), 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                                                                             len(body), 2835, 2835, 0, 0)
    Path(bmp_path).write_bytes(head + body)
    return bmp_path


if __name__ == "__main__":                                           # quick try-out:  python portrait_generator.py "female elf" "adult female elf, cleric"
    s = load_settings()
    subj = sys.argv[1] if len(sys.argv) > 1 else "female elf"
    char = sys.argv[2] if len(sys.argv) > 2 else "adult female elf, pointed ears, cleric, priestly vestments"
    pr, ng = build_prompt(s, subj, char)
    print(pr, "\n\nNEGATIVE:", ng, "\n")
    try:
        got = Engine(s, print).generate(pr, ng, int(s.get("candidates", 2)))
        for q in save_candidates(got, HERE.parent / str(s.get("output_folder", "generated")) / "tryout", subj):
            print("saved", q)
    except GeneratorError as err:
        print("ERROR:", err)
