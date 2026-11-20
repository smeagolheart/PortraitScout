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
        "dull ochre and soft grey-brown",
        "faded red-brown and deep umber",
        "smoky gold and warm brown",
        "dusty olive and warm grey",
        "deep green-grey and dull ochre",
        "muted teal and warm shadow",
        "desaturated slate blue and pale stone",
        "muted indigo and bone white",
        "dull brick red and dark umber",
        "deep moss green and weak amber",
    ],
    "_background_palettes": "Colour schemes for the background, used wherever the background text says {random}. "
                            "One is picked at random per picture so a few thousand portraits do not all sit "
                            "against the same wash. All of them are deliberately muted and weighted towards earth "
                            "colours, the way the game's own portraits are - a bright background pulls the eye "
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
                      "Painted in thick matte oils: broken colour, visible brush edges, dry-brush texture, no "
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
                      "clothing - broken colour, visible brush edges on the cheek and brow, no smooth blending - "
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
    "use_creature_colours": True,
    "_use_creature_colours": "The creature file says what colour its sprite's clothes, hair and skin are. true: "
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
        "Old": "old, lined face, grey hair", "Ancient": "very old, deeply wrinkled, thin white hair",
    },
    "look_terms": {
        "_about": "Offered as 'Look' in the Generate window; the text is added to the end of the description. "
                  "Flux paints clean skin unless it is told otherwise, so these are worth using - and the "
                  "blemishes have to be named one by one, because 'weathered' on its own is not enough.",
        "Ordinary": "",
        "Weathered": "a deeply lined weather-beaten face, furrowed brow and heavy crow's feet, sun-darkened "
                     "uneven skin with visible pores and broken veins across the nose and cheeks, grey-flecked "
                     "stubble, shadowed eye sockets, dry chapped lips",
        "Blotchy and freckled": "blotchy ruddy skin heavily freckled across the nose and cheeks, patches of red "
                                "and sallow colour, flaking dry skin, pitted pores, uneven stubble, yellowed "
                                "crooked teeth",
        "Gaunt and old": "a gaunt deeply wrinkled old face, hollow cheeks and sunken temples, sagging skin, liver "
                         "spots and broken veins, heavy eye bags, thin grey hair and straggly beard, sallow "
                         "weathered colour",
        "Scarred": "an old scar across the face, rough pitted skin",
        "Stern": "a stern, hard expression",
        "Friendly": "a warm, friendly expression, smiling eyes",
        "Sad": "a tired, downcast expression",
        "Fierce": "a fierce, angry expression, bared teeth",
        "Angry": "an angry, scowling expression, furrowed brow",
        "Sickly": "pale, gaunt and unwell, grey blotchy skin, dark rings under the eyes",
        "Noble": "clean, well-kept, proud bearing",
    },
    "bald_animations": ["fat"],
    "_bald_animations": "Words from the creature's animation name whose sprite is bald (the 'Fat Man' sprite "
                        "several merchants use). These win over the creature file, which can give a hair colour "
                        "to a creature whose sprite has no hair. Anything else with a hair colour is never drawn "
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
            "greying hair swept back",
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
        "elf": "elf with subtly pointed ears",
        "half elf": "half-elf with slightly pointed ears",
        "dwarf": "dwarf, stocky",
        "halfling": "halfling",
        "gnome": "gnome",
        "half orc": "half-orc, pale green skin, heavy brow, muscular",
        "sirine": "sirine, a sea fey woman, green skin, long dark hair wet and tangled with seaweed, large dark "
                  "eyes, a serene unsettling beauty",
        "doppleganger": "doppelganger, a half-formed grey humanoid with blank featureless eyes, smooth unfinished "
                        "features caught between faces",
        "imp": "imp, a small red devil, bat wings, little horns, a barbed tail, a sly pointed face, yellow eyes",
        "demonic": "demon, a monstrous fiend with a beast's head, horns, burning eyes, dark hide, bared fangs",
        "rakshasa": "rakshasa, a tiger's head on a man's shoulders, orange and black striped fur, amber eyes, "
                    "rich embroidered robes",
        "mind flayer": "mind flayer, a purple-grey octopus head with four writhing face tentacles, white pupilless "
                       "eyes, no nose",
        "beholder": "beholder, a floating sphere of flesh with one huge central eye and eyestalks writhing above "
                    "it, a wide toothed maw",
        "minotaur": "minotaur, a bull's head on a man's shoulders, heavy curved horns, a brass nose ring, "
                    "shaggy brown fur",
        "svirfneblin": "deep gnome, small and wiry, grey stone-coloured skin, large black eyes, a bald head, a "
                       "wispy grey beard",
        "genie": "djinni, a towering blue-skinned spirit of the air, white eyes with no pupils, a pointed beard, "
                 "the lower body trailing away into smoke",
        "fairy": "fey creature, small and sharp-featured, bright inhuman eyes, pointed ears, a faint glow",
        "golem": "golem, a mute figure built of stone and iron, blank carved features, no eyes, seams and rivets",
        "mephit": "mephit, a small winged imp of elemental stuff, a mischievous pinched face, little bat wings",
        "myconid": "myconid, a walking fungus, a domed mushroom cap for a head, mottled spotted skin, tiny dark eyes",
        "gibberling": "gibberling, a small hunched shrieking creature, a mouth too wide, matted fur, wild eyes",
        "kuo-toa": "kuo-toa, a fish-headed humanoid, bulging silver eyes on the sides of the head, scaled grey "
                   "skin, a lipless mouth of needle teeth",
        "yuanti": "yuan-ti, a serpent-headed humanoid, green and gold scales, slit yellow eyes, a forked tongue",
        "solar": "solar, a towering winged celestial, burning golden eyes, skin like polished bronze, great "
                 "white wings",
        "planatar": "planetar, a tall winged celestial with smooth green-white skin, a bald head, white feathered "
                    "wings, pupilless eyes",
        "ogrillon": "ogrillon, a half-ogre brute, coarse grey-brown skin, a heavy misshapen jaw, small eyes",
        "wererat": "wererat, a rat's head on a man's shoulders, patchy brown fur, a long snout, yellow incisors, "
                   "pink ears",
        "revenant": "revenant, a corpse come back, grey tight skin, sunken burning eyes, old wounds unhealed",
        "ghast": "ghast, a ravening corpse, grey rotting skin, bared teeth, sunken eyes, lank clumps of hair",
        "spectre": "spectre, a ghostly undead spirit, hollow eyes lit from within, gaunt drawn face, "
                   "colourless bloodless skin, wisps of mist where the shoulders fade away",
        "spectral undead": "spectral undead, a ghostly spirit, hollow eyes lit from within, gaunt drawn face, "
                           "colourless bloodless skin, the edges fading into mist",
        "wraith": "wraith, a hooded shape of darkness, no face but two points of cold light, tattered shroud",
        "ghost": "ghost, a pale ghostly figure, hollow eyes, bloodless colourless skin, edges fading away",
        "shadow": "living shadow, a man-shaped darkness, two cold points of light for eyes",
        "vampire": "vampire, bloodless white skin, red eyes, long fangs, sunken cheeks",
        "ghoul": "ghoul, grey rotting skin stretched over the skull, sunken eyes, bared teeth, patchy hair",
        "zombie": "zombie, grey rotting skin, clouded eyes, slack jaw, matted hair",
        "skeleton": "skeleton, a bare skull and bones, empty eye sockets, no flesh at all",
        "lich": "lich, a skull face with shrivelled grey skin drawn tight over it, points of light in the sockets",
        "mummy": "mummy, dried blackened skin wrapped in filthy bandages, sunken sockets",
        "orc": "orc, green skin, a heavy jutting brow and small eyes, a broad flat nose, jutting lower tusks, pointed ears, coarse black hair",
        "goblin": "goblin, sickly green skin, huge pointed ears, a long hooked nose, small yellow eyes, sharp teeth",
        "hobgoblin": "hobgoblin, orange-red skin, a flat broad face with a heavy jutting brow, dark beady eyes, a "
                     "blue-grey flattened nose, pointed ears, yellowed teeth, wearing a battered "
                     "horned rusty steel helmet",
        "kobold": "kobold, small reptilian head, rust-red scaled skin, a short snout, horns, beady red eyes",
        "gnoll": "gnoll, hyena head, coarse spotted fur, a long muzzle with bared teeth, dark eyes",
        "ogre": "ogre, huge lumpen head, dull yellow-brown skin, a heavy jaw and small eyes, tusks",
        "troll": "troll, long rubbery green head, a hooked nose, sunken eyes, lank hair",
        "lizard man": "lizard man, green scaled head, a blunt snout, slit yellow eyes, a crest of spines",
        "sahuagin": "sahuagin, green-black fish head, round black eyes, gill slits, needle teeth, a finned crest",
        "githyanki": "githyanki, gaunt yellow-skinned head, sunken black eyes, pointed ears, a hairless skull",
        "duergar": "grey dwarf, ashen grey skin, a bald head, a long grey beard, hard pale eyes",
        "tiefling": "tiefling, small horns",
        "drow": "drow elf, dark grey skin, white hair, pointed ears",
        "aasimar": "aasimar, fair luminous skin, pale golden eyes",
        "half ogre": "half-ogre, broad coarse face, dull ruddy skin, a heavy jaw, small tusks",
        "lycanthrope": "werewolf, a wolf's head on a man's shoulders, coarse grey fur, yellow eyes, bared fangs",
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
        return _merge(DEFAULTS, json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
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
_HAIR_COLOUR = re.compile(r"\b(greying|graying|grey|gray|white|black|blond|blonde|red|auburn|brown|silver)\s+"
                          r"(?=hair|mane|curls|locks)", re.I)


def features(key: str, settings: dict, has_hair: bool = False, bald: bool = False) -> str:
    """One face, nose and hair for this creature, and sometimes a distinguishing mark - always the same ones.
    has_hair: the creature file gives a hair colour, so it has hair - don't draw it bald.
    bald: its sprite is bald, whatever the file says - draw it bald."""
    if not key:
        return ""
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
            out.append("a bald head" if slot == "hair" and bald else pick)
    marks = [t for t in (terms.get("mark") or []) if str(t).strip()]
    try:
        chance = float(settings.get("feature_mark_chance", 0.35))
    except (TypeError, ValueError):
        chance = 0.35
    if marks and rng.random() < chance:
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
    said_kind = str((row.get("colours") or {}).get("kind") or "")    # DrowSkin and the like: the surest of all
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
        ("young" if aw & {"boy", "girl", "child"} else "adult")
    subject = " ".join(x for x in (gender, race_txt.split(",")[0]) if x) or "person"
    lead, _, extra = age_txt.partition(",")                          # "old, lined face" -> "old male human, lined face"
    parts = [" ".join(x for x in (lead.strip(), gender, race_txt) if x)]
    if extra.strip():
        parts.append(extra.strip())
    manlike = rkey in ("", "human", "elf", "half elf", "dwarf", "halfling", "gnome", "half orc", "tiefling",
                       "drow", "aasimar", "duergar", "genasi")
    col = row.get("colours") or {} if settings.get("use_creature_colours", True) else {}
    bald = bool(aw & {str(w).lower() for w in (settings.get("bald_animations") or [])
                      if not str(w).startswith("_")})            # the sprite is bald; the file does not know it
    feat = features(face_key(row), settings, bool(col.get("hair")), bald) if manlike else ""
    #                                                            a neat side parting on a gnoll helps nobody
    if col.get("hair"):
        feat = _HAIR_COLOUR.sub("", feat)                        # the file says what colour it is; the draw only
    said = parts[0].lower()                                      # says what it does. What the race already covers
    covered = not manlike or any(w in said for w in ("fur", "scale", "hide", "feather", "pelt", "undead",
                                                     "ghostly", "translucent", "skeleton", "bones", "rotting",
                                                     "bandages", "shadow", "mist"))
    #   the colour bytes are a sprite's palette: on a face with no skin and no hair - a mind flayer's tentacles,
    #   a doppelganger's blank grey - they are nonsense. What it wears still comes through.
    #                                                            not a face with skin and
    col = {k: v for k, v in col.items() if k != "kind"}          # a label, not something to say
    for slot in ("skin", "hair"):                                # hair on it. Right behind the race: at the end
        if not col.get(slot) or slot in said or covered:         # of a long description the model ignores colour
            continue
        if slot == "hair" and (any(w in feat.lower() for w in _BALD)     # no hair colour on a bald head, nor
                               or any(w in said for w in _COVERED_HEAD)):   # under a helm the sprite never takes off
            continue
        parts.append(col[slot])
    took_role = any(w in race_txt.lower() for w in ("ghostly", "translucent", "mist", "darkness"))
    #                                                            nothing is wearing armor inside a spirit, and the
    #                                                            sprite's class says only which BAM was borrowed
    for word, phrase in settings.get("animation_terms", {}).items():
        if word.startswith("_") or word not in aw or phrase in parts:
            continue
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
    return text.replace("{random}", random.choice(choices) if choices else "muted colour")


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

    def generate(self, prompt: str, negative: str, count: int,
                 on_image: Callable[[int, bytes], None] = lambda i, b: None,
                 seed: Optional[int] = None) -> List[bytes]:
        """`count` candidate images (PNG bytes), one request each so they can be shown as they arrive."""
        self.start()
        payload = {k: v for k, v in self.s.get("payload", {}).items() if not k.startswith("_")}
        payload.update(prompt=prompt, negative_prompt=negative, n_iter=1)
        base = int(seed) if seed is not None else -1                 # the creature's own seed, so a re-run matches
        out: List[bytes] = []
        count = max(1, int(count))
        for i in range(count):
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
                "undead spirit", "made of mist", "column of pale grey smoke", "living shadow")


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

        grey = im.convert("L")                                   # the eyes: the brightest spots in the top half
        hist, total, cut = grey.histogram(), w * h, 255
        seen = 0
        for v in range(255, -1, -1):                             # the brightest 2% of the picture
            seen += hist[v]
            if seen >= total * 0.02:
                cut = v
                break
        spots = grey.point(lambda v: 255 if v >= max(cut, 170) else 0)
        top = Image.new("L", (w, h), 0)
        top.paste(255, (0, 0, w, int(h * 0.55)))
        glow = ImageChops.multiply(spots, top).filter(ImageFilter.GaussianBlur(max(1, w // 90)))
        out = Image.composite(im, out, glow)
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
    except (OSError, ValueError):
        return data


SKIN_TINTS = {                                                   # name: (hue in degrees, how much colour)
    "Green": (110, 1.30), "Grey": (210, 0.55), "Ashen white": (200, 0.35), "Blue": (220, 1.25),
    "Red-orange": (18, 1.55), "Yellow-brown": (40, 1.30), "Purple-grey": (280, 0.75), "Sea green": (150, 1.20),
}


def suggest_tint(text: str) -> str:
    """Which tint the description is asking for, if any - so the button opens on the right colour."""
    t = (text or "").lower()
    for word, name in (("ashen", "Ashen white"), ("charcoal", "Grey"), ("grey skin", "Grey"), ("gray skin", "Grey"),
                       ("green skin", "Green"), ("sea-green", "Sea green"), ("sea green", "Sea green"),
                       ("orange-red skin", "Red-orange"), ("red skin", "Red-orange"), ("blue skin", "Blue"),
                       ("purple-grey", "Purple-grey"), ("yellow-brown", "Yellow-brown"), ("sirine", "Green"),
                       ("orc", "Green"), ("hobgoblin", "Red-orange"), ("drow", "Grey")):
        if word in t:
            return name
    return ""


SPOT_TINTS = {                           # name: (hue in degrees, how colourful to make it, how bright; -1 = leave)
    "Leave": (0, 0, -1), "Amber": (35, 185, -1), "Green": (110, 165, -1), "Blue": (215, 175, -1),
    "Pale blue": (205, 85, -1), "Violet": (280, 160, -1), "Red": (5, 205, -1), "Gold": (45, 195, -1),
    "Copper": (20, 165, -1), "Teal": (175, 160, -1), "Pink": (330, 150, -1), "Silver": (210, 25, 200),
    "Black": (0, 0, 35), "White": (0, 0, 235),
}
EYE_TINTS = SPOT_TINTS                   # it began as an eye colour; it works on anything you click
LIP_TINTS = {                                                    # name: (hue in degrees, how much colour)
    "Leave": (0, 0.0), "Red": (6, 1.70), "Dark red": (2, 1.35), "Berry": (340, 1.45), "Pink": (352, 1.25),
    "Pale": (20, 0.55), "Grey": (210, 0.45), "Green": (110, 1.30), "Black": (0, 0.20),
}


def touch_up(data: bytes, hue: float = 110, amount: float = 0.0, sat: float = 1.3, catch: float = 0.6,
             light: float = 0.0, lip_hue: float = 6, lip_amount: float = 0.0, lip_sat: float = 1.7,
             spots: Optional[List[Tuple[float, float]]] = None, eye_hue: float = 35, eye_amount: float = 0.0,
             eye_sat: float = 185, eye_size: float = 0.035, eye_val: float = -1) -> bytes:
    """Work on the skin of a finished picture: its colour, how dark it is, and the lips on their own.

    The engine paints a person it recognises whatever the description says, so this is done afterwards. Skin is
    not found by looking for flesh colours - a picture can have a colour cast of its own, and a creature's skin
    may be green or grey already. The face's own hue is read off the picture first (the middle hue inside the
    oval a face sits in, ignoring the dark and the strongly coloured), and pixels near that hue are worked on,
    along with the warm band that lips, nose and ears sit in. catch widens both. light makes the skin darker or
    lighter, from -1 to 1. The lips are the warm, coloured part of that, and can be given a colour of their own.
    """
    if amount <= 0 and lip_amount <= 0 and abs(light) < 0.01 and not (spots and eye_amount > 0):
        return data
    try:
        import io
        from PIL import Image, ImageChops, ImageDraw, ImageFilter
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
        cal = sc.point(lambda x: 255 if 10 <= x <= int(90 + catch * 120) else 0)   # not gold, not gems
        here = ImageChops.multiply(ImageChops.multiply(oval.point(lambda x: 255 if x > 128 else 0), lit), cal)
        hist, run, mid = hc.histogram(here), 0, 0
        total = sum(hist) or 1
        for i, n in enumerate(hist):                             # the face's own hue, whatever colour it is
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
            k = 1.0 + max(-1.0, min(1.0, light)) * 0.60
            vc2 = Image.composite(vc.point(lambda x: max(0, min(255, int(round(x * k))))), vc, face)
        if lip_amount > 0:                                       # the lips: the coloured part of the lower face,
            mouth = Image.new("L", im.size, 0)                   # and not the nose, the ears or a red gem
            ImageDraw.Draw(mouth).ellipse((w * 0.26, h * 0.46, w * 0.74, h * 0.74), fill=255)
            mouth = mouth.filter(ImageFilter.GaussianBlur(max(1, w // 40)))
            lips = ImageChops.multiply(ImageChops.multiply(warm, sc.point(lambda x: 255 if x >= 70 else 0)),
                                       ImageChops.multiply(lit, mouth))
            ltgt = int(round(lip_hue % 360 / 360 * 255))
            hc2 = Image.composite(hc.point(lambda x: int(round(x + (ltgt - x) * min(1.0, lip_amount)))), hc2, lips)
            sc2 = Image.composite(sc.point(lambda x: max(0, min(255, int(round(x * lip_sat))))), sc2, lips)
        if spots and eye_amount > 0:                             # a spot has to be pointed at: an iris is no
            rad = max(2.0, eye_size * w)                         # different in colour from a cheek. Inside the
            etgt = int(round(eye_hue % 360 / 360 * 255))         # ring, though, the iris can be found: read what
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
                # What is under the click is the iris: take its colour and brightness from a few pixels at the
                # centre, then keep what matches inside the ring. Lashes are far darker, the white far brighter
                # and the lid a different colour, so all three drop out without having to be described.
                px = max(1, int(rad * 0.22))
                patch = im.crop((int(cx - px), int(cy - px), int(cx + px + 1), int(cy + px + 1))).convert("HSV")
                hs, ss, vs = (sum(c[i] for c in patch.getdata()) / max(1, len(patch.getdata())) for i in range(3))
                near_v = vc.point(lambda x: 255 if abs(x - vs) <= 62 else 0)
                near_h = hc.point(lambda x: 255 if abs(((x - hs + 128) % 256) - 128) <= 40 else 0)
                grey_ = sc.point(lambda x: 255 if x < 36 else 0)     # a grey iris has no hue worth matching
                iris = ImageChops.multiply(ImageChops.multiply(ring, near_v),
                                           ImageChops.lighter(near_h, grey_ if ss < 60 else grey_.point(lambda x: 0)))
                iris = iris.filter(ImageFilter.GaussianBlur(max(1, int(rad * 0.12))))   # no hard speckled edges
                a_e = min(1.0, eye_amount)
                if eye_sat > 0:                                  # move the colour there, do not scale what is
                    hc2 = Image.composite(hc.point(lambda x: int(round(x + (etgt - x) * a_e))), hc2, iris)
                sc2 = Image.composite(sc.point(lambda x: max(0, min(255, int(round(x + (eye_sat - x) * a_e))))),
                                      sc2, iris)                 # there: a grey iris has nothing to scale up
                if eye_val >= 0:
                    vc2 = Image.composite(vc.point(lambda x: max(0, min(255, int(round(x + (eye_val - x) * a_e))))),
                                          vc2, iris)
                elif eye_sat > 0:                                # a hue on a near-black iris is still near-black:
                    floor = 70 + a_e * 55                        # lift it enough for the colour to be seen
                    vc2 = Image.composite(vc.point(lambda x: int(x + (floor - x) * a_e * 0.75) if x < floor else x),
                                          vc2, iris)
        buf = io.BytesIO()
        Image.merge("HSV", (hc2, sc2, vc2)).convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    except (OSError, ValueError):
        return data


def skin_tint(data: bytes, hue: float = 110, amount: float = 0.9, sat: float = 1.3, catch: float = 0.6) -> bytes:
    """Kept for older callers: the skin colour part of touch_up()."""
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
    """(width, height, RGB bytes) of an 8-bit RGB / RGBA / grey PNG that is not interlaced. No Pillow needed."""
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
        else:                                                        # grey (+ alpha)
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
