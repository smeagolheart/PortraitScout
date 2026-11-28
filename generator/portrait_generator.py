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
        "muted warm brown and terracotta",
        "pale cream and soft tan",
        "dull ochre and soft gray-brown",
        "faded red-brown and deep umber",
        "smoky gold and warm brown",
        "dusty olive and warm gray",
        "deep green-gray and dull ochre",
        "muted teal and warm shadow",
        "desaturated slate blue and pale gray",
        "muted indigo and bone white",
        "dull rust red and dark umber",
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
                      "(:.35 -> :.65) or crop with Head zoom. Its trigger word 'b2g8' is left out on purpose: it pulls hard "
                      "towards the faces it was trained on and drops things like a tiefling's horns.",
            "zoom": 1.0,
            "prompt": "A character from a medieval fantasy world, in clothes of the Middle Ages. Extreme close-up face portrait of {character}, cropped at the collarbone. The head alone "
                      "fills the picture: the hair nearly touches the top edge, the face reaches from edge to edge, "
                      "the chin sits in the middle, and only a sliver of collar and shoulder shows along the bottom. "
                      "Nothing below the collarbone, no chest, no armor plates, no hands, no weapons. Head upright, "
                      "eyes on the viewer. Painted as a classic Baldur's Gate character portrait: a realistic oil "
                      "painting with visible brushstrokes and canvas texture, realistic facial proportions, natural "
                      "skin tones, detailed weathered skin, expressive lifelike eyes, deep rich colors, warm light "
                      "from one side with soft shadows. <lora:Baldurs_Gate_Portrait_Flux:.35> Behind the head, "
                      "{background}."
        },
        "Klein painterly": {
            "_about": "Written for Forge Neo with FLUX.2 Klein 9B (4 steps, CFG 1), and carrying no LoRA on "
                      "purpose: the painterly LoRA has a face baked into it, so every seed paints the same "
                      "person. Klein paints nearly the same face for the same words whatever the seed anyway, "
                      "which is what Vary faces and the face bank are for.",
            "zoom": 1.0,
            "background": "softly blurred painterly background with warm {random} earthy blue green tone "
                          "muted backdrop",
            "prompt": "A character from a medieval fantasy world, in clothes of the Middle Ages. stylized "
                      "digital art, head and shoulders, large head focus, close up depth of field, rich "
                      "vibrant colors, detailed eyes, detailed skin texture and pores, crisp facial details, "
                      "artstation trending, weathered appearance, out-of-focus painterly background: "
                      "{background}, subtle chiaroscuro lighting, faint indistinct shadows, fantasy oil "
                      "painting style, facial wrinkles and detail, {character}",
        },
        "Animal (no person)": {
            "_about": "For a portrait of an actual animal - a cat, a dog, a horse - rather than a person. The "
                      "other looks ask for a character portrait, and at full portrait size (taller than wide) "
                      "the oil-painting LoRA and its trigger word fill the body below the head with a "
                      "character's clothes, so a cat came out as a cat-headed person. This one leaves the "
                      "trigger word out, keeps the LoRA light (.4) for the brushwork only, and says up front "
                      "that it is a real animal with its own body down to the bottom edge. Flux at cfg 1 "
                      "ignores the negative prompt, so the positive prompt has to do all of it.",
            "zoom": 1.0,
            "negative_prompt": "(picture frame:1.6), (border:1.6), (framed picture:1.6), (photograph:1.5), "
                               "(3d render:1.5), anthropomorphic, humanoid, clothing, armor, human face, hands",
            "prompt": "An oil painting of a real animal and nothing else: {character}. Close-up of its head "
                      "and shoulders, as a naturalist would paint it from life - its natural animal body, fur "
                      "or feathers down to the bottom edge, animal eyes looking at the viewer. Painted in thick "
                      "matte oils: broken color, visible brush edges, dry-brush texture. "
                      "<lora:chargen_oil_painting_v2:.4> Behind the head, {background}, kept off the face.",
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
            "prompt": "A character from a medieval fantasy world, in clothes of the Middle Ages. Extreme close-up face portrait of {character}, cropped at the collarbone. The head alone "
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
            "prompt": "A character from a medieval fantasy world, in clothes of the Middle Ages. Close-up head and shoulders portrait of {character}. The face fills most of the frame, front "
                      "view, centered. Painterly fantasy character portrait in the manner of classic Dungeons & Dragons "
                      "book illustrations and Baldur's Gate portraits: a realistic oil painting with visible brushstrokes "
                      "and rich canvas texture, realistic facial proportions, natural skin tones, detailed weathered "
                      "skin, expressive lifelike eyes, deep rich colors, warm light from one side with soft shadows. "
                      "<lora:Baldurs_Gate_Portrait_Flux:.55> Set against {background}."
        },
        "Painterly, lighter LoRA": {
            "zoom": 1.0,
            "prompt": "A character from a medieval fantasy world, in clothes of the Middle Ages. Close-up head and shoulders portrait of {character}. The face fills most of the frame, front "
                      "view, centered. Painterly fantasy character portrait in the manner of classic Dungeons & Dragons "
                      "book illustrations and Baldur's Gate portraits: a realistic oil painting with visible brushstrokes "
                      "and rich canvas texture, realistic facial proportions, natural skin tones, detailed weathered "
                      "skin, expressive lifelike eyes, deep rich colors, warm light from one side with soft shadows. "
                      "<lora:Baldurs_Gate_Portrait_Flux:.3> Set against {background}."
        },
    },
    "monster_edits": {"_about": "The second pass for monsters started from the face bank: what each is made into, in words. {skin}: the creature's own skin color, when its file has one.", "half-ogre": "a coarse brutish half-ogre: a much heavier jutting brow, small deep-set eyes, a thicker, wider jaw with two big tusks jutting up over the lip, a broad flat nose, rough thick {skin}", "orc": "a brutish orc: {skin}, a heavy jutting brow, small eyes, a broad flat pig-like nose, a heavy jaw with two big tusks jutting up over the lip, pointed ears", "orog": "a brutish orog: {skin}, an enormous long jaw with two big tusks, a heavy jutting brow", "hobgoblin": "a brutish hobgoblin: {skin}, large pointed ears, a heavy brow, a broad flat nose, a wide mouth of small yellowed fangs", "goblin": "a scrawny goblin: {skin}, very large pointed ears, a long sharp nose, small mean eyes, a wide mouth of small sharp teeth", "xvart": "a small wiry xvart: {skin}, large pointed ears, bright orange eyes, a wide mouth of small sharp teeth", "gnoll": "", "troll": "", "githyanki": "a gaunt githyanki: {skin}, a hard, hollow-cheeked face, small swept-back pointed ears"},
    "face_bank": {
        "_about": "Start from: a picture starts from a real face (img2img) so that faces differ - the model paints "
                  "nearly one face per description whatever the seed. folder = where the faces are, beside "
                  "Portrait Scout unless a full path: one folder per race (human, elf, dwarf...), human used for any "
                  "race without its own, child only for children. strength = how far from the face the picture may "
                  "go. steps = the steps a picture from the bank is painted in: the engine runs steps x strength of "
                  "them, so 6 at 0.68 costs what 4 plain ones do, and 4 steps leave nothing between keeping the "
                  "photo (0.7) and ignoring it (0.75). A folder may hold male and female folders; the picture's "
                  "gender picks one. scale = how big the face is painted against the bank picture (the bank's are a "
                  "little bigger than a painted head and shoulders). skip = races left to their words unless they have a folder of their own: a "
                  "human face holds a dwarf's to human proportions and shaves his beard.",
        "monster": {"strength": 0.74, "steps": 8, "blur": 0, "hulk_strength": 0.85, "edit": 0.85},
        "folder": "faces", "strength": 0.68, "steps": 6, "scale": 0.8, "skip": ["duergar", "svirfneblin"],
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
        "Child": "child", "Young": "young", "Adult": "", "Middle-aged": "middle-aged",
        "Old": "old, lined face, gray hair", "Ancient": "very old, deeply wrinkled, thin white hair",
    },
    "look_terms": {
        "_about": "Offered as 'Look' in the Propose tab: the state of the face only (the expression is Mood, "
                  "the clothes are Outfit). It goes right after the race. Flux paints clean skin unless it is told otherwise, so these are worth using - and the "
                  "blemishes have to be named one by one, because 'weathered' on its own is not enough.",
        "Ordinary": "",
        "Weathered": "a deeply lined weather-beaten face, furrowed brow and heavy crow's feet, sun-darkened "
                     "uneven skin with visible pores and broken veins across the nose and cheeks, gray-flecked "
                     "stubble, shadowed eye sockets, dry chapped lips",
        "Blotchy and freckled": "blotchy ruddy skin heavily freckled across the nose and cheeks, patches of red "
                                "and sallow color, flaking dry skin, pitted pores, uneven stubble, yellowed "
                                "crooked teeth",
        "Sickly": "pale, gaunt and unwell, gray blotchy skin, dark rings under the eyes",
        "Well-kept": "a clean, well-kept face, neatly groomed",
    },
    "scar_terms": {
        "_about": "Offered as 'Scars' in the Propose tab, from none to fresh wounds. scar_default is used when "
                  "nothing is picked (a creature's own description, the gallery's Generate). Klein paints the "
                  "words as written, so each step names a little more.",
        "None": "",
        "Few": "a thin pale healed scar on one cheek",
        "Scarred": "a pale healed scar across the brow and cheek",
        "Heavy": "a heavily scarred face, many pale healed scars across the cheeks, brow and lips",
        "Wounded": "fresh bleeding cuts across the face, a split lip, dried blood on the cheek",
    },
    "scar_default": "Few",
    "grime_terms": {
        "_about": "Offered as 'Grime' in the Propose tab: how dirty the face is.",
        "Clean": "",
        "Smudged": "a faint smudge of dirt on one cheek",
        "Dirty": "a dirty face, smudges of dirt and dust on the cheeks and brow",
        "Filthy": "a filthy face smeared with soot and grime, streaked with sweat",
    },
    "edit_terms": {
        "_about": "Right-click a picture > Change expression / Change it...: the picture is repainted from itself in "
                  "edit mode (the model keeps the person and changes only what it is told). keep = what is said "
                  "after the change; expressions = the list offered, name: words.",
        "keep": "Paint the whole picture crisp and sharp, the neck, shoulders and clothes in full detail with no haze or smudge. Keep everything else the same: the same person, pose, lighting, background and painting "
                "style.",
        "expressions": {"Neutral": "a calm, neutral expression", "Smiling": "a warm smile", "Stern": "a stern, hard look", "Sad": "a sad, downcast look", "Anxious": "an anxious, worried look with a furrowed brow", "Tired": "a tired, weary look", "Smug": "a smug, sly half-smile", "Surprised": "a surprised look, raised eyebrows", "Angry": "an angry scowl"},
    },
    "mood_terms": {
        "_about": "Offered as 'Mood' in the Propose tab: the expression. Any says nothing, and then Vary "
                  "faces picks one for each picture, mostly calm ones (vary_terms expression).",
        "Any": "",
        "Neutral": "a calm, neutral expression",
        "Stern": "a stern, hard expression",
        "Friendly": "a warm, friendly expression, smiling eyes",
        "Sad": "a tired, downcast expression",
        "Angry": "an angry, scowling expression, bared teeth",
        "Fierce": "a furious snarl, bared teeth",
    },
    "beard_terms": {"By race": "", "None": "a smooth, clean-shaven chin and upper lip", "Stubble": "stubble", "Short": "a short beard", "Full": "a full beard", "Long": "a long beard", "Braided": "a braided beard"},
    "_beard_terms": "Offered as 'Beard' in the Propose tab, for men. By race: what race_picks gives the race (a dwarf "
                    "his own beard), else nothing said. None names a smooth chin and lip rather than 'no beard': "
                    "naming a beard, even to deny it, paints one.",
    "role_terms": {
        "_about": "Offered as 'Role' in the Propose tab, one for each kind of person the games have a sprite for. "
                  "who: the words for the person. outfit: one of outfit_terms, what they wear unless Outfit says "
                  "otherwise. also: other words in an animation name that mean this role. The first role word in "
                  "the animation name wins, so 'Harlot Fighter' and 'Peasant Woman Fighter' are not fighters.",
        "Fighter": {"who": "fighter", "outfit": "Padded armor"},
        "Cleric": {"who": "cleric", "outfit": "Vestments", "also": ["priest"]},
        "Mage": {"who": "mage", "outfit": "Robes", "also": ["wizard", "sorcerer"]},
        "Thief": {"who": "rogue", "outfit": "Leather", "also": ["rogue"]},
        "Monk": {"who": "monk", "outfit": "Robes"},
        "Soldier": {"who": "soldier", "outfit": "Mail", "also": ["guard"]},
        "Noble": {"who": "noble", "outfit": "Fine", "also": ["nobleman", "noblewoman", "aristocrat"]},
        "Peasant": {"who": "peasant", "outfit": "Peasant", "also": ["commoner", "farmer", "laborer"]},
        "Beggar": {"who": "beggar, thin and underfed", "outfit": "Rags", "also": ["slave", "poor"]},
        "Sailor": {"who": "sailor, salt-weathered", "outfit": "Plain", "also": ["pirate"]},
        "Courtesan": {"who": "courtesan", "outfit": "Fine", "also": ["harlot"]},
    },
    "outfit_terms": {
        "_about": "Offered as 'Outfit' in the Propose tab: what the character wears. 'By role' takes the role's "
                  "own. {color} is the creature's main clothes color and {trim} 'with red trim' when it has a "
                  "second one; both drop out when the file gives none. A value can be {\"male\": ..., "
                  "\"female\": ...}. Name the garment - a tunic, a gambeson, a gown: plain 'clothes' in a "
                  "color is painted as a modern shirt.",
        "By role": "",
        "Rags": "torn, patched {color} rags",
        "Plain": {"male": "a plain faded {color} wool tunic{trim}", "female": "a plain faded {color} wool kirtle{trim}"},
        "Peasant": {"male": "a coarse homespun {color} wool tunic", "female": "a coarse homespun {color} wool kirtle"},
        "Fine": {"male": "a rich embroidered {color} velvet doublet{trim}",
                 "female": "a rich embroidered {color} velvet gown{trim}"},
        "Robes": "a high-collared {color} robe{trim}",
        "Vestments": "{color} priestly vestments{trim}",
        "Leather": "a fitted jerkin of supple {color} leather, laced at the front",
        "Padded armor": "a worn {color} padded gambeson",
        "Mail": "a mail shirt under a {color} tabard",
        "Plate armor": "plate armor over a {color} surcoat",
    },
    "sprite_terms": {
        "_about": "Animations that show a body or gear of their own, so the portrait matches the figure walking "
                  "around. Matched on the words of the animation name ('Static Fat Man' is a Fat Man; the longest "
                  "match wins, so an Ogre Mage is not an Ogre). text: what the sprite shows. front: true puts it "
                  "before the race ('very fat ... male human') - a body said late is painted ordinary. bald: "
                  "the sprite has no hair, whatever the creature file says. Hoods and headscarves belong here, "
                  "on the sprites that wear them, not in an outfit - not every peasant wears one. outfit: what it wears - an "
                  "outfit_terms name, or \"\" for nothing past the text (a beast in its own hide and helmet). "
                  "Left out, the role decides as usual. Never name a beard to say there is none: it paints one.",
        "Fat Man": {"text": "very fat, bald, clean-shaven: a huge, round, fleshy face, wider than it is long, fat cheeks and jowls, a double chin, a thick neck, a smooth shaven head, a smooth bare chin and upper lip", "front": True, "bald": True, "outfit": "Plain"},
        "Fat Woman": {"text": "very fat, heavy-set: a broad, round, fleshy face, plump cheeks, a double chin, a "
                              "thick neck", "front": True, "outfit": "Plain"},
        "Peasant Woman": {"text": "a plain headscarf"},
        "Thief": {"text": "a deep hood pulled up over the head"},
        "Harlot Woman": {"text": "her hair pulled up into a high ponytail, a medieval off-the-shoulder linen chemise with a front-laced bodice over it, the shoulders bare", "outfit": ""},
        "Orog": {"text": "a grotesquely long horse-like face twice as long as a man's with a huge long jaw, one pair of huge long pointed ears set high on the sides of the skull and sweeping straight upward, their tips rising well above the crown of the bald head, dull reddish-orange skin, heavy black spiked iron shoulder plates over a dark leather harness", "bald": True, "outfit": ""},
        "Kobold Xvart": {"text": "a sleeveless brown leather jerkin, bare blue arms", "race": "xvart", "outfit": ""},
        "Noble Woman": {"text": "a noblewoman's tall stiff embroidered cloth headdress rising into two soft pointed peaks above her forehead, a fifteenth-century lady's escoffion of padded fabric with a sheer veil, her long hair hanging down behind it, a wide collar over the shoulders, the headdress and the collar both the color of the gown's trim"},
        "Noble Man": {"text": "a wide floppy velvet cap, a Renaissance beret worn tilted to one side, with a long curling plume feather pinned to it"},
        "Beggar Man": {"text": "bald, with a straggly unkempt beard, gaunt and bare-chested: no shirt, bony bare shoulders and chest", "bald": True, "outfit": ""},
        "Boy": {"text": "", "child": True, "race": "human"},
        "Girl": {"text": "", "child": True, "race": "human"},
        "Ogre": {"text": "a small head sunk low between massive hunched shoulders, an enormous fat and muscular body filling the frame, a bare, hairless bald head, a crude sleeveless brown leather vest over a bare chest", "bald": True, "skin_colors": True, "outfit": ""},
        "Ogre Mage": {"text": "a small head sunk low between massive hunched shoulders, an enormous fat and muscular body filling the frame, a bald head with two short curved horns on the brow and a tied high ponytail behind the head, layered robes with padded shoulder pieces", "bald": True, "skin_colors": True, "outfit": ""},
        "Half Ogre": {"text": "wearing a battered horned iron helmet", "outfit": "", "skin_colors": True},
        "Ogrillon": {"text": "", "outfit": "", "skin_colors": True},
        "Hobgoblin": {"text": "wearing a battered horned rusty steel helmet", "outfit": ""},
    },
    "young_lead": "A young {person} of about twenty, with youthful smooth skin.",
    "child_lead": "A {person} of about nine years old, a small child with a soft, round child's face, smooth skin and big eyes, a small slight child's body with narrow little shoulders and a thin neck.",
    "young_drop": ["weathered appearance", "facial wrinkles and detail"],
    "_young_lead": "For a young character (Age: Young, or 'young' at the start of the description): young_lead goes "
                   "first in the prompt and the young_drop words are taken out of the style, or a style asking for "
                   "a weathered, wrinkled face paints everybody forty.",
    "vary_terms": {
        "pose": {"": 70, "head turned slightly to the left, a slight three-quarter view": 10, "head turned slightly to the right, a slight three-quarter view": 10, "head tilted slightly to one side": 5, "eyes glancing slightly off to one side": 5},
        "_about": "Vary faces on the Propose tab: each picture of a Generate gets one of each of these, drawn at "
                  "random, added to the description. Klein paints nearly the same face for one description "
                  "whatever the seed, so a face has to be told apart in words. A list is skipped when the "
                  "description already says it (an age, a hair color, the eyes).",
        "age": ["about twenty years old", "about thirty years old", "about forty years old",
                "about fifty years old", "about sixty years old"],
        "face": ["a heart-shaped face", "a gaunt hollow-cheeked face", "a broad square jaw", "a long narrow face",
                 "a round soft face", "a sharp angular face", "a lean weathered face", "a heavy-jowled face"],
        "nose": ["a small upturned nose", "a hooked nose", "a flattened broken nose", "a long straight nose",
                 "a wide flat nose", "a crooked nose", "a strong aquiline nose", "a bulbous nose"],
        "mouth": ["full lips", "thin lips", "a wide mouth", "a small tight mouth", "a crooked half-smile",
                  "a downturned mouth"],
        "eyes": ["wide-set green eyes", "deep-set gray eyes", "heavy-lidded dark eyes", "pale blue eyes",
                 "narrow amber eyes", "warm brown eyes"],
        "hair": {"short neat hair": 14, "close-cropped hair": 12, "short untidy hair": 12, "shaggy hair to the ears": 10, "a plain short cut": 8, "wavy hair to the jaw": 8, "curly hair": 6, "hair tied back in a short tail": 6, "shoulder-length hair": 6, "long loose hair": 4, "hair shaved at the sides": 3, "a tangled mane": 3, "short spiky hair": 2, "a single braid": 1, "dreadlocks": 5, "hair bound with leather cords and small beads": 2},
        "hair_female": {"long loose hair": 14, "hair tied back in a tail": 12, "wavy hair to the shoulders": 12, "hair pinned up in a knot": 10, "long straight hair": 10, "curly hair": 8, "a thick plait over one shoulder": 6, "short practical hair": 5, "a messy braid": 4, "a tangled mane": 3, "dreadlocks": 5, "long hair with small beaded tassels": 2},
        "hair_color": {"brown": 40, "black": 30, "dark brown": 15, "blond": 8, "auburn": 4, "red": 3},
        "_hair_color": "How often each hair color comes up, out of the total. Gray and white are not here: they "
                       "come with age - old_hair_color is used instead once the face is fifty or older.",
        "old_hair_color": {"gray": 45, "graying brown": 25, "white": 15, "salt-and-pepper": 15},
        "brows": ["heavy brows", "thin arched brows", "bushy brows", "straight level brows", "sparse pale brows",
                  "a scarred eyebrow", "", ""],
        "skin": {"fair": 30, "tanned": 20, "ruddy": 15, "olive": 15, "pale": 10, "light brown": 6, "dark brown": 4},
        "cloth_color": {"undyed": 20, "brown": 20, "gray-green": 12, "faded blue": 12, "dull red": 8, "ochre": 8,
                        "dark gray": 10, "rust": 6, "moss green": 4},
        "cloth_detail": ["patched sleeves", "a frayed collar", "a wide leather belt", "a woolen shawl over the shoulders",
                         "a cord necklace with a wooden charm", "a simple brooch at the collar", "rolled-up sleeves",
                         "a laced neckline", "a pouch strap across the chest", "a scarf knotted at the throat",
                         "a rough cloak pinned at the shoulder", "a fur-trimmed collar", "a hood pushed back",
                         "a knotted kerchief", "", ""],
        "cloth_detail_armor": ["dented pauldrons", "a scuffed leather strap across the chest",
                               "a cloak pinned at the shoulder", "a battered gorget", "a frayed tabard",
                               "rivets and rust on the armor", "a wool scarf under the collar", "a fur mantle"],
        "beast": {"age": {"young": 25, "": 45, "old and grizzled": 30}, "expression": {"a calm, watchful look": 30, "an alert, wary look": 25, "a menacing glare": 20, "a hungry look": 15, "bared teeth": 10}, "mark": ["a torn ear", "an old scar across the face", "a missing tooth", "a clouded eye", "a notched ear", "", "", ""], "detail": ["", "", "", "patchy, uneven coloring", "a darker streak across the brow", "lighter coloring around the eyes"]},
        "undead": {"expression": {"a vacant, hungry stare": 30, "a lipless grin": 20, "a menacing glare": 25, "a slack, dead look": 25}, "mark": ["a patch of bare bone on the cheek", "a torn, missing ear", "tattered strips of dead skin", "a cracked jaw", "one eye socket empty", "rotted holes in the cheek", "", ""], "detail": ["", "", "grave dirt in the creases", "a few teeth missing", "a pale, faintly glowing gaze"]},
        "_beast": "Vary faces for beasts and the undead (werewolves, kobolds, lizard men, ghouls, zombies...): their face is their race's, so they are told apart by temper, age and marks, put right after the race's name.",
        "monster": {"expression": {"a dull, sullen look": 20, "a cunning, sly look": 15, "a fierce glare": 15, "a calm, watchful look": 20, "a toothy sneer": 10, "a weary look": 10, "a crooked grin": 10}, "age": {"young": 25, "in his prime": 40, "grizzled and older": 35}, "face": ["a broad, flat face", "a gaunt, bony face", "a heavy, jowly face", "a long, narrow face", "a lumpy, warty face", "a lean, hard face"], "long_face": ["a long, gaunt face with sunken cheeks", "a long, heavy, jowly face", "a long, lumpy, warty face", "a long, bony face with a jutting underbite", "a long face with a pointed, bony chin", "a long, lean, hard face with deep-set eyes", "a long, coarse face with a wide mouth", "a long, craggy face with a heavy chin"], "mark": ["a torn ear", "a notched ear", "a missing tooth", "a broken fang", "a cloudy blind eye", "an old scar across the nose", "heavy brow ridges", "a crooked broken nose", "", ""], "detail": ["tufts of coarse hair on the ears", "a ring through one ear", "tribal paint on the cheeks", "a bone stud in the nose", "", "", ""]},
        "_monster": "Vary faces for goblins, orcs, ogres and the like: their face is their race's, so they are told apart by temper, age, build and marks - never a person's nose, eyes or skin, which made them men painted red.",
        "_cloth": "Vary faces dresses each picture a little differently too: cloth_color when the description "
                  "names no color (the creature file's color always wins), and one cloth_detail - the armor "
                  "list for anyone in armor. brows and skin go with the face; skin only for humans and others "
                  "whose description does not say their skin.",

        "mark": ["freckles", "a pockmarked face", "an old scar on the chin", "a mole on the cheek", "", "", ""],
        "expression": {"a calm, neutral expression": 32, "a faint, polite smile": 15, "a thoughtful, attentive look": 13, "a warm smile": 10, "a guarded, wary look": 8, "a stern, serious look": 8, "a tired, weary look": 7, "a sly smirk": 4, "a broad grin": 3},
        "_expression": "How often each expression comes up, out of the total: mostly calm, since most of them are just talking to you. Angry ones are left out on purpose: pick Angry or Fierce as the Mood for those.",
    },
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
    "race_picks": {
        "_about": "Things that differ between creatures of one race, by weight, added to its race words: a drow's "
                  "eye color, a dwarf's beard. Each creature gets the same one every time (drawn from its own "
                  "seed). A list called beard is for men only. An empty choice adds nothing (a gnome without a "
                  "beard).",
        "drow": {"eyes": {"red eyes": 60, "violet eyes": 25, "pale lavender eyes": 15}},
        "dwarf": {"beard": {"a long, full, bushy beard reaching halfway down his chest": 35, "a long braided beard reaching halfway down his chest": 25, "a long beard reaching halfway down his chest, bound with metal rings": 25, "a long forked beard reaching halfway down his chest": 15}},
        "gnome": {"nose": {"a very large, long, pointed nose": 25, "a big bulbous nose": 20, "a long hooked nose": 15, "a broad round nose": 15, "a long upturned nose": 10, "a big crooked nose": 15}, "ears": {"big ears tapering to points": 40, "large pointed ears sticking out sideways": 30, "long swept-back pointed ears": 15, "big ears with round-tipped points": 15}, "eyes": {"bright twinkling eyes under bushy eyebrows": 25, "small sharp eyes under wild white eyebrows": 15, "large curious eyes": 20, "heavy-lidded sleepy eyes": 10, "keen dark eyes under thick brows": 20}, "lines": {"deep wrinkles and laugh lines": 30, "apple cheeks": 20, "a deeply lined forehead": 15, "crow's feet": 15, "": 20}, "beard": {"a short wispy beard": 50, "a thin goatee": 30, "": 20}},
        "kobold": {"hide": {"rust-red scaly hide": 35, "dull brick-red scaly hide": 20, "dark red-brown scaly hide": 20, "mottled orange and brown scaly hide": 15, "faded gray-red scaly hide": 10}, "horns": {"two small backswept horns": 40, "two short stubby horns": 25, "long curved horns": 20, "one horn broken off short": 15}, "eyes": {"small beady red eyes": 50, "small beady yellow eyes": 25, "small beady orange eyes": 25}, "mark": {"": 40, "a scar across the snout": 20, "a chipped tooth": 15, "a ragged frill behind the jaw": 10, "a notched horn": 15}},
        "halfling": {"face": {"a broad, round face with rosy cheeks": 30, "a broad face with freckled cheeks": 20, "a broad, weathered face": 25, "a broad, plump face with a double chin": 15, "a broad face with a strong jaw": 10}, "hair": {"a head of thick tight curls": 35, "loose brown curls": 20, "short curly hair": 20, "curly hair tied back": 10, "thinning curly hair": 15}, "nose": {"a button nose": 40, "a small upturned nose": 25, "a broad snub nose": 20, "": 15}},
        "hobgoblin": {"skin": {"coarse dusky orange-brown hide-like skin": 25, "rough brick-red skin": 20, "dark reddish-brown skin": 20, "mottled gray-orange skin": 15, "ruddy ochre skin": 20}, "nose": {"a swollen red tinted nose with thin blue veins": 25, "a flat, wide nose": 30, "a short upturned snout-like nose": 30, "a broad squashed nose": 15}, "eyes": {"small dark beady eyes": 30, "narrow yellow eyes": 25, "deep-set amber eyes": 20, "small red-rimmed eyes": 15, "pale watery eyes": 10}, "beard": {"a scraggly black beard": 25, "a thin drooping black mustache": 15, "coarse black stubble": 20, "a braided chin tuft": 10, "": 30}},
        "goblin": {"skin": {"sallow yellowish skin": 30, "pale yellow-green skin": 25, "light green skin": 20, "mustard yellow skin": 15, "dull yellow-ochre skin": 10}, "ears": {"large pointed ears sticking out sideways": 40, "very large batlike ears": 25, "long drooping pointed ears": 20, "big ragged notched pointed ears": 15}, "nose": {"a long hooked nose": 30, "a sharp pointed nose": 25, "a stubby snub nose": 20, "a warty bulbous nose": 25}, "eyes": {"small yellow eyes": 30, "beady red eyes": 20, "large bulging yellow eyes": 20, "narrow orange eyes": 15, "squinting dark eyes": 15}, "hair": {"a bald head with a few wiry hairs": 35, "a greasy black mop of hair": 15, "a thin wispy topknot": 20, "patchy gray stubble on the scalp": 15, "stringy dark hair": 15}},
        "ogre": {"face": {"a heavy brow and a huge jutting jaw": 30, "a low sloping forehead": 20, "sagging jowls": 20, "a thick bull neck": 15, "": 15}, "eyes": {"small dumb mean eyes": 30, "tiny piggish eyes": 25, "deep-set dark eyes under a heavy brow": 20, "bloodshot yellow eyes": 15, "mismatched eyes, one larger": 10}, "nose": {"a flat smashed nose": 30, "a broad bulbous nose": 25, "a huge warty nose": 20, "a pig-like upturned nose": 15, "": 10}, "beard": {"": 40, "coarse black stubble": 20, "a ragged dark beard": 20, "a few long chin whiskers": 20}},
        "duergar": {"beard": {"a long white beard": 60, "a braided white beard": 25, "a short white beard": 15}},
    },
    "_tusks": "Tusks are said as lower canines growing up from inside the mouth: said loosely ('jutting tusks'), "
              "Klein grows them out of the chin. Half-orcs have none.",
    "race_terms_female": {
        "_about": "Used instead of race_terms for a woman of these races. Flux paints any woman as a slim pretty human "
                  "unless told plainly otherwise, so a dwarf or gnome woman came out as a human girl with braids "
                  "(braids made it worse). Saying her age and that she is homely rather than pretty is what moves it.",
        "dwarf": "dwarf, a stout, broad-shouldered dwarf woman in her fifties, homely and rugged rather than pretty: "
                 "a very broad, chubby, round face, wider than it is long, heavy fleshy ruddy cheeks, a double chin, "
                 "a thick neck, a big broad nose, a heavy brow",
        "duergar": "duergar, a gray dwarf woman from the deep underdark, stout and broad-shouldered, grim and homely rather than pretty: dark charcoal-gray skin with a faint purple sheen, a broad heavy face, deep-set pale eyes under a heavy brow, a big broad nose, stark white hair pulled back tight",
        "gnome": "gnome, a grown-up gnome woman, homely and quirky rather than pretty",
    },
    "race_terms": {
        "_about": "How a race from the game is worded in the prompt. Races not listed are used as they are, which is rarely enough for a monster: a bare \"orc\" is painted as a human with pointed ears, so say what makes one.",
        "human": "human",
        "elf": "elf, an ageless elf: long ears that sweep up to fine points, a narrow face with high sharp cheekbones, a pointed chin, large almond-shaped eyes, smooth fine skin",
        "half elf": "half-elf, a mostly human face touched with elven blood: ears only slightly pointed at the tips, fine high cheekbones",
        "dwarf": "dwarf, short, stout and heavy-boned, a grown-up: a broad, plump face, wider than it is long, heavy round ruddy cheeks, a big broad nose, a heavy brow",
        "halfling": "halfling",
        "gnome": "gnome, a grown-up of the gnome folk, not a child",
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
        "ghast": "ghast, a ravening undead corpse-eater, not a living man: a gaunt skull-like head, gray-green rotting skin stretched tight over the bones, no nose but two slits, a lipless mouth of long jagged yellow teeth, deep sunken sockets with small burning eyes, lank clumps of hair",
        "spectre": "spectre, a ghostly undead spirit, hollow eyes lit from within, gaunt drawn face, "
                   "colorless bloodless skin, wisps of mist where the shoulders fade away",
        "spectral undead": "spectral undead, a ghostly spirit, hollow eyes lit from within, gaunt drawn face, "
                           "colorless bloodless skin, the edges fading into mist",
        "wraith": "wraith, a hooded shape of darkness, no face but two points of cold light, tattered shroud",
        "ghost": "ghost, a pale ghostly figure, hollow eyes, bloodless colorless skin, edges fading away",
        "shadow": "living shadow, a man-shaped darkness, two cold points of light for eyes",
        "vampire": "vampire, bloodless white skin, red eyes, long fangs, sunken cheeks",
        "ghoul": "ghoul, an undead corpse-eater, not a living man: a gaunt skull-like head, gray-green rotting skin stretched tight over the bones, no nose but two slits, a lipless mouth of long jagged yellow teeth, deep sunken sockets with small glowing pale eyes, pointed ears, a few wisps of lank hair",
        "zombie": "zombie, gray rotting skin, clouded eyes, slack jaw, matted hair",
        "skeleton": "skeleton, a bare skull and bones, empty eye sockets, no flesh at all",
        "lich": "lich, a skull face with shrivelled gray skin drawn tight over it, points of light in the sockets",
        "mummy": "mummy, dried blackened skin wrapped in filthy bandages, sunken sockets",
        "orc": "orc, green skin, a heavy jutting brow and small eyes, a broad flat nose, two prominent tusks growing up from the lower jaw inside the mouth, in place of the lower canine teeth, resting over the upper lip, pointed ears, coarse black hair",
        "goblin": "goblin, a small, scrawny goblin, not a lizard: a wide mouth of small sharp teeth",
        "hobgoblin": "hobgoblin, a brutish goblinoid with a wide face, large pointed ears and small yellowed fangs",
        "xvart": "xvart, a small, wiry goblin-like humanoid, not a kobold and not a lizard: bright blue skin, a bald domed blue head with a ring of dark hair around the back and sides, large pointed ears, bright orange eyes, a wide mouth of small sharp teeth",
        "kobold": "kobold, a small dog-like reptile creature, not a human: a long narrow lizard snout with the nostrils at its tip, jaws lined with tiny sharp teeth",
        "gnoll": "gnoll, hyena head, coarse spotted fur, a long muzzle with bared teeth, dark eyes",
        "ogre": "close up of an ugly ogre: a broad humanoid face, a wide mouth with two thick tusks growing up from the lower jaw inside the mouth, in place of the lower canine teeth, resting over the upper lip",
        "troll": "troll, long rubbery green head, a hooked nose, sunken eyes, lank hair",
        "lizard man": "lizard man, green scaled head, a blunt snout, slit yellow eyes, a crest of spines",
        "sahuagin": "sahuagin, green-black fish head, round black eyes, gill slits, needle teeth, a finned crest",
        "githyanki": "githyanki, a gaunt alien warrior with jaundiced, leathery yellow skin: a hard, fierce, hollow-cheeked face with high sharp cheekbones, a flat slitted nose, deep-set fierce black eyes, small dark speckles scattered across the cheeks and brow, long swept-back pointed ears, a stiff black topknot on a close-shorn head",
        "duergar": "duergar, a gray dwarf from the deep underdark, short, stout and heavy-boned: dark charcoal-gray skin with a faint purple sheen, a broad, hard, grim face, deep-set pale eyes under a heavy brow, a big broad nose, a bald head",
        "tiefling": "tiefling, a devil-blooded human, two curved horns rising from the brow",
        "drow": "drow, a dark elf with smooth charcoal-black skin with a faint purple sheen, long pointed ears, stark white hair",
        "aasimar": "angelic human, fair luminous skin, pale golden eyes, a faint halo of light",
        "half ogre": "half-ogre, broad coarse face, dull ruddy skin, a heavy jaw, two small tusks growing up from the lower jaw inside the mouth, in place of the lower canine teeth",
        "lycanthrope": "werewolf, a wolf's head on a man's shoulders, coarse gray fur, yellow eyes, bared fangs",
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


BY_ROLE = "By role"


def roles(settings: dict) -> dict:
    """role_terms without its notes: {"Fighter": {"who": ..., "outfit": ..., "also": [...]}, ...}"""
    return {k: v for k, v in (settings.get("role_terms") or {}).items()
            if not str(k).startswith("_") and isinstance(v, dict)}


def find_role(words: object, settings: dict) -> str:
    """The role an animation name says ("CLERIC_FEMALE_ELF" -> "Cleric"), or "". The first role word in the name
    wins: a 'Harlot Fighter' is a courtesan and a 'Nobleman Fighter' a noble, both of them sprites of their own."""
    seq = [str(w).lower() for w in words] if isinstance(words, (list, tuple)) else \
        re.findall(r"[a-z]+", str(words).lower())
    table = roles(settings)
    for w in seq:
        for name, v in table.items():
            if w == name.lower() or w in {str(a).lower() for a in (v.get("also") or [])}:
                return name
    return ""


def sprite(anim: str, settings: dict) -> dict:
    """The sprite_terms entry for an animation name, the longest match first, or {}."""
    aw = set(re.findall(r"[a-z]+", anim.lower()))
    best: Tuple[int, dict] = (0, {})
    for k, v in (settings.get("sprite_terms") or {}).items():
        ks = set(re.findall(r"[a-z]+", str(k).lower()))
        if not str(k).startswith("_") and isinstance(v, dict) and ks and ks <= aw and len(ks) > best[0]:
            best = (len(ks), v)
    return best[1]


def outfit_text(settings: dict, outfit: str, gender: str = "", color: str = "", trim: str = "") -> str:
    """An outfit's words with the creature's colors put in, or dropped when there are none."""
    t = (settings.get("outfit_terms") or {}).get(outfit, "")
    if isinstance(t, dict):
        t = t.get(gender) or t.get("male") or next(iter(t.values()), "")
    t = str(t or "")
    if not t:
        return ""
    t = t.replace("{color}", color).replace("{trim}", f" with {trim} trim" if trim else "")
    return " ".join(t.split()).replace(" ,", ",")


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
_SUBRACE = {"drow": "elf", "duergar": "dwarf", "svirfneblin": "gnome"}   # a kind the race field can only call
#                                                                  by its parent race
_NAME_BLIND = frozenset(("shadow",))    # the Shadow Thieves are a guild of people, not forty-five living shadows
_VAGUE_RACES = frozenset(("human", "fairy", "demonic", "monster", "animal", "mammal", "no race", "undead",
                          "giant", "golem", "elemental", "statue"))
_COVERED_HEAD = ("helm", "hood", "coif", "cowl", "turban", "veil")
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


_FACIAL_HAIR = re.compile(r"\b(beard|bearded|stubble|mustache|moustache|whiskers|goatee|sideburns)\b", re.I)


def _no_facial_hair(text: str) -> str:
    """A look's words without the beard: "thin gray hair and straggly beard" -> "thin gray hair"."""
    out = []
    for clause in (c.strip() for c in text.split(",")):
        if not _FACIAL_HAIR.search(clause):
            out.append(clause)
            continue
        keep = [b for b in re.split(r"\s+and\s+|\s+with\s+", clause) if not _FACIAL_HAIR.search(b)]
        if keep:
            out.append(" and ".join(keep))
    return ", ".join(c for c in out if c)


def womanly(desc: str) -> str:
    """A woman's description without beards or stubble, whatever wrote them in - a saved description from before
    the looks were fixed said "thin gray hair and straggly beard" of an old elf woman, and every picture had one."""
    if not re.search(r"\b(female|woman|girl|lady)\b", desc or "", re.I):
        return desc
    return _no_facial_hair(desc)


def youthen(prompt: str, desc: str, settings: dict) -> str:
    """A young character in a style that asks for a weathered, wrinkled face comes out forty. Said first, and with
    the style's aging words taken out (young_drop), Klein paints the age asked for. A child is said the same way."""
    low = desc.lower()
    if re.search(r"^(a )?child\b|\bchild (male|female)\b|^(girl|boy)\b", low):
        who = "girl" if re.search(r"\b(female|girl)\b", low) else "boy" if re.search(r"\b(male|boy)\b", low) else "child"
        for w in (settings.get("young_drop") or []):
            prompt = re.sub(r"\s*" + re.escape(str(w)) + r",?", "", prompt, flags=re.I)
        lead = str(settings.get("child_lead") or "").replace("{person}", who)
        return (lead + " " + prompt).strip() if lead else prompt
    if not re.search(r"^(young|a young)\b|\byoung (male|female|man|woman)\b|\babout twenty years old\b", low):
        return prompt
    who = "woman" if re.search(r"\b(female|woman)\b", low) else "man" if re.search(r"\b(male|man)\b", low) else "person"
    for w in (settings.get("young_drop") or []):
        prompt = re.sub(r"\s*" + re.escape(str(w)) + r",?", "", prompt, flags=re.I)
    lead = str(settings.get("young_lead") or "").replace("{person}", who)
    return (lead + " " + prompt).strip() if lead else prompt


def _weighted(rnd: object, pool: object, skip: tuple = ()) -> str:
    """One pick from {"brown": 40, "red": 3, ...} by weight; a plain list counts every entry the same."""
    if isinstance(pool, dict):
        items = [(str(k), float(v)) for k, v in pool.items() if not str(k).startswith("_") and str(k) not in skip
                 and isinstance(v, (int, float)) and v > 0]
    else:
        items = [(str(k), 1.0) for k in (pool or []) if str(k) not in skip]
    if not items:
        return ""
    return str(rnd.choices([k for k, _ in items], weights=[w for _, w in items])[0])   # type: ignore[attr-defined]


_BEASTLY = re.compile(r"\b(were\w*|lycanthrope|wolf|wolf's|fur|furred|muzzle|snout|fangs|beak|feathers?|"
                      r"tentacles?|hyena|gnoll|lizard|fish|scales|scaled|insect|mandibles|rat|animal|beast|"
                      r"not of a person|skeleton|skull|bones|undead spirit|mind flayer|beholder|golem|elemental|"
                      r"slime|ooze|dragon|wyvern|spider|cat|dog|hound|horse|bear|boar|deer|stag|rabbit|squirrel|chicken|cow|calf|"
                      r"bird|owl|raven|eagle|snake|bat|beetle|panther|leopard|tiger|lion|ape|baboon|monkey|"
                      r"ghoul|ghast|zombie|mummy|lich|revenant|wight|corpse|undead)\b")
#   the undead too: a grizzled age, a heavy jowly face and an ear ring made a ghoul an old man


_UNDEAD = re.compile(r"\b(ghoul|ghast|zombie|mummy|lich|revenant|wight|corpse|undead|skeleton|skull|spectre|ghost|"
                     r"wraith|vampire)\b")
_MONSTROUS = re.compile(r"\b(goblin|hobgoblin|xvart|orc|ogre|ogrillon|half-ogre|troll|githyanki|gnoll|bugbear|"
                        r"minotaur|yuan-ti|imp|demon|devil|tanar'ri)\b")
#   a face given in full by its race: a person's nose, eyes, skin tone and haircut on it make it a man painted red


def _vary_face(rnd: object, settings: dict, desc: str = "") -> str:
    """A face in words, drawn at random, for one picture of a Generate: age, face, nose, mouth, eyes, hair and
    sometimes a mark. Whatever the description already says is left out - its age, its hair color, its eyes -
    so it is never contradicted."""
    terms = settings.get("vary_terms") or {}
    low = desc.lower()
    if _MONSTROUS.search(low) and terms.get("monster"):            # (fangs or hide do not make it a beast)
        mt = terms.get("monster") or {}                         # a goblin's face kept a goblin's: told apart by
        bits = []                                                # build, age, temper and the marks of a hard life
        long_ = bool(re.search(r"\b(huge|enormous|long|elongated)\b[^,]{0,30}\bface\b", low))   # a sprite's own
        ears_ = bool(re.search(r"\bears\b[^,]{0,60}\b(top of|upward|crown)\b", low))     # long face
        for k, skip in (("age", r"\b(young|old|aged|grizzled|prime)\b"),     # still varies, within its length; ears
                        ("long_face" if long_ else "face", ""),            # on top get no ear marks, or a second
                        ("expression", ""),                                # pair is drawn at the sides
                        ("mark", ""), ("detail", "")):              # face and temper said first win over the race's
            pool = mt.get(k) or (mt.get("face") if k == "long_face" else None)
            if pool and ears_:
                pool = ({w: n for w, n in pool.items() if not re.search(r"\bears?\b", w)} if isinstance(pool, dict)
                        else [w for w in pool if not re.search(r"\bears?\b", w)])
            if pool and not (skip and re.search(skip, low)):
                bits.append(_weighted(rnd, pool) if isinstance(pool, dict) else rnd.choice(pool))   # type: ignore[attr-defined]
        clothes = vary_clothes(rnd, settings, desc)
        face = ", ".join(b for b in bits if b)
        face = "!" + face if face else ""                        # ! = right after the race's name, not after its words
        return f"{face} || {clothes}" if clothes else face
    if _BEASTLY.search(low) or _MONSTROUS.search(low):          # a werewolf given a person's eyes, hair, nose and
        undead = bool(_UNDEAD.search(low))                       # skin tone comes out a man: told apart by its own
        mt = terms.get("undead" if undead else "beast") or {}    # kind of words instead - temper, age, marks
        bits = []
        for k, skip in (("age", r"\b(young|old|aged|grizzled|ancient)\b"), ("expression", ""), ("mark", ""), ("detail", "")):
            pool = mt.get(k)
            if pool and not (skip and re.search(skip, low)):
                bits.append(_weighted(rnd, pool) if isinstance(pool, dict) else rnd.choice(pool))   # type: ignore[attr-defined]
        clothes = vary_clothes(rnd, settings, desc)
        face = ", ".join(b for b in bits if b)               # after the race's own words: what it is comes first
        return f"{face} || {clothes}" if clothes else face

    def pick(k: str) -> str:
        if isinstance(terms.get(k), dict):                       # {"a calm look": 30, ...}: by weight
            return _weighted(rnd, terms.get(k))
        pool = [str(x) for x in (terms.get(k) or [])]
        return rnd.choice(pool) if pool else ""                                       # type: ignore[attr-defined]
    out = []
    if not re.search(r"\b(young|elderly|teen|child|twenties|thirties|forties|fifties|sixties|years)\b|"
                     r"^(very |middle-)?(old|aged)\b|\bold,?\s+(male|female|man|woman)\b", low):   # not "an old scar"
        out.append(pick("age"))
    youngish = bool(re.search(r"\b(young|teen|twenty|twenties|thirty|thirties|child|girl|boy|nine)\b", low + " " + " ".join(out)))
    woman = bool(re.search(r"\b(female|woman|girl|lady|she|her)\b", low))
    for k in ("face", "nose", "mouth"):
        if k not in low or k == "face" and "face" not in low:
            got = pick(k)
            for _ in range(8):                                   # a young face is not weathered or jowly
                if not (youngish and re.search(r"weather|gaunt|lined|jowl|wrinkl|sagging|haggard", got)):
                    break
                got = pick(k)
            out.append(got)
    if "eyes" not in low:
        out.append(pick("eyes"))
    own_hair = [str(x).lower() for x in ((settings.get("feature_terms") or {}).get("hair") or []) if str(x).strip()]
    styled = any(h in low for h in own_hair) or any(re.sub(r"\b(hair|dark|light|[a-z]+-?(brown|gray|grey|white|black|blond|blonde|red))\b|"
                        r"\b(red|black|gray|grey|blond|blonde|brown|auburn|white|silver|sandy|ginger|golden|"
                        r"copper|chestnut|fair|raven|flaxen|platinum|steel|and|a|an)\b", "", c).strip(" -")
                 for c in low.split(",") if re.search(r"\bhair\b", c))   # the creature's own hairstyle is said
    if not styled and not re.search(r"\b(bald|shaven head|hairless|helmet|hood)\b", low):
        hair = pick("hair_female" if woman and terms.get("hair_female") else "hair")
        has_color = bool(re.search(r"\b(red|black|gray|grey|blond|blonde|brown|auburn|white|silver|sandy|ginger|"
                                       r"golden|copper|chestnut|fair|dark|raven|flaxen|platinum)\s+hair\b|"
                                       r"\bhair dyed\b", low))          # the creature file's color wins
        if hair and not has_color:
            said_age = low + " " + " ".join(out)                 # the person's age, not "an old scar"
            oldish = not youngish and bool(re.search(
                r"\b(elderly|ancient|fifty|fifties|sixty|sixties|seventy)\b|"
                r"\bold,?\s+(male|female|man|woman|\w+\s+(male|female))\b|^(very )?old\b", said_age))
            col = _weighted(rnd, terms.get("old_hair_color" if oldish and terms.get("old_hair_color") else "hair_color"),
                            () if oldish else ("gray", "white") if youngish else ())
            hair = re.sub(r"\bhair\b", f"{col} hair", hair, count=1) if "hair" in hair else f"{col} hair, {hair}"
        out.append(hair)
    if not re.search(r"\b(child|girl|boy)\b", low):              # pockmarks and scars are for grown-ups
        out.append(pick("mark"))
    if "brow" not in low:
        out.append(pick("brows"))
    if "skin" not in low and re.search(r"\b(human|half elf|halfling|man|woman|boy|girl)\b", low):
        out.append(f"{_weighted(rnd, terms.get('skin'))} skin" if terms.get("skin") else "")
    if not re.search(r"\b(grin|smil|frown|snarl|scowl|expression|look|smirk|stare|squint)", low):
        out.insert(0, pick("expression"))                    # the strongest of them all: a face moves with it
    face = ", ".join(x for x in out if x)
    clothes = vary_clothes(rnd, settings, desc)
    return f"{face} || {clothes}" if clothes else face



_POSED = re.compile(r"\b(turned|three-quarter|profile|side view|looking (left|right|away|over)|glancing|tilted)\b")


def vary_words(rnd: object, settings: dict, desc: str = "") -> str:
    """A face in words for one picture (see _vary_face), and now and then a slight turn of the head from
    vary_terms' pose list - most portraits face straight ahead, as the game's do, so only a few turn."""
    words = _vary_face(rnd, settings, desc)
    pose = _weighted(rnd, (settings.get("vary_terms") or {}).get("pose") or {})
    if not pose or _POSED.search(desc.lower()):
        return words
    face, sep, clothes = words.partition(" || ")
    face = f"{face}, {pose}" if face.strip("! ") else (f"!{pose}" if face.startswith("!") else pose)
    return f"{face} || {clothes}" if sep else face


_CLOTH_COLORED = re.compile(
    r"\b(red|blue|green|brown|gray|grey|black|white|yellow|orange|purple|gold|silver|tan|ochre|undyed|dyed|"
    r"crimson|maroon|teal|olive|russet|indigo)\b[\w\s-]{0,30}?\b(wool|linen|velvet|leather|cloak|robes?|tunic|kirtle|"
    r"gown|doublet|tabard|surcoat|gambeson|vestments|rags|clothes|armor)\b", re.I)
_ARMORED = re.compile(r"\b(mail|plate|gambeson|helmet|helm|metal armor)\b", re.I)   # leather is not: a pauldron
#                                                                  or gorget on a thief's leathers paints plate


def vary_clothes(rnd: object, settings: dict, desc: str = "") -> str:
    """Clothes in words for one picture: a color when the description gives none, and a detail - patched
    sleeves, a shawl, a belt. Klein paints the same outfit for the same words, so it has to be told apart too."""
    terms = settings.get("vary_terms") or {}
    if not re.search(r"\b(wool|linen|velvet|leather|cloak|robes?|tunic|kirtle|gown|doublet|tabard|surcoat|gambeson|"
                     r"vestments|rags|clothes|armor|mail)\b", desc, re.I):
        return ""                                            # nothing worn to vary: a beast, a spirit
    out = []
    if not _CLOTH_COLORED.search(desc) and terms.get("cloth_color"):
        c_ = _weighted(rnd, terms.get("cloth_color"))
        out.append("undyed clothes" if c_ == "undyed" else f"clothes dyed {c_}" if c_ else "")
    pool = [str(x) for x in (terms.get("cloth_detail_armor" if _ARMORED.search(desc) else "cloth_detail") or [])]
    low_ = desc.lower()                                          # no second hood on a hooded thief, no scarf on a scarf
    pool = [x for x in pool if not any(w in low_ for w in ("hood", "scarf", "kerchief", "cloak", "mantle", "collar")
                                       if w in x.lower())] or pool
    if pool:
        out.append(rnd.choice(pool))                                                  # type: ignore[attr-defined]
    return ", ".join(x for x in out if x)


def vary_merge(desc: str, words: str, settings: Optional[dict] = None) -> str:
    """The description with one picture's Vary words put in: the face right after the race, where Klein looks
    hardest (said last, much of it was ignored), and the clothes at the end, after the outfit they change.
    The race's own words are kept together: a dwarf's beard pushed behind a dozen face words came out shaven."""
    face, _, clothes = (words or "").partition("||")
    face, clothes = face.strip().strip(","), clothes.strip().strip(",")
    if face.startswith("!"):                                     # a monster's: first, where Klein looks hardest -
        face = face[1:].strip()                                  # behind a dozen fixed words it changed nothing
        head, sep, rest = (desc or "").partition(",")
        out = f"{head}, {face}" + (f",{rest}" if sep else "")
        return out + (", " + clothes if clothes else "")
    if face and settings:
        texts = [str(v) for k in ("race_terms", "race_terms_female")
                 for kk, v in (settings.get(k) or {}).items() if not str(kk).startswith("_") and v]
        hit = max((t for t in texts if t and t in (desc or "")), key=len, default="")
        if hit:
            at = desc.index(hit) + len(hit)
            spr = re.match(r"(,\s*(wearing [^,]*|a plain headscarf|a [^,]*\bhood\b[^,]*|[a-z ]+ eyes))+", desc[at:])   # the race's
            #                                                      eyes and the sprite's own gear go with it
            if spr:
                at += spr.end()
            extras = sorted({str(c) for v in (settings.get("race_picks") or {}).values() if isinstance(v, dict)
                             for pool in v.values() if isinstance(pool, dict) for c in pool if c}
                            | {str(v) for k, v in (settings.get("beard_terms") or {}).items()
                               if v and not str(k).startswith("_")}, key=len, reverse=True)
            moved = True
            while moved:                                         # a dwarf's beard, a drow's eyes: the race's own too
                moved = False
                for x in extras:
                    if desc[at:].startswith(", " + x):
                        at += len(x) + 2
                        moved = True
                        break
            out = desc[:at].rstrip(", ") + ", " + face + desc[at:]
            return out + (", " + clothes if clothes else "")
    parts = [c.strip() for c in (desc or "").split(",")]
    if face and parts and parts[0]:
        at = next((i for i, c in enumerate(parts) if re.search(r"\b(male|female|man|woman|boy|girl)\b", c)), 0)
        at = at if ":" in parts[at] else at + 1              # "clean-shaven male human: a huge, round face" is
        parts.insert(at, face)                               # one body: the face words go before it, not inside
    elif face:
        parts = [face]
    if clothes:
        parts.append(clothes)
    return ", ".join(p for p in parts if p)


def describe(row: dict, settings: dict, age: str = "", look: str = "", outfit: str = "",
             mood: str = "", scars: str = "", grime: str = "", beard: str = "") -> Tuple[str, str]:
    """(subject, character) from a Portrait Scout row: gender, race and the animation name (which says what the
    speaker wears: 'Cleric Female Elf', 'Peasant Woman', 'Fat Man'). Nothing here is certain - it is a starting
    point the user can edit before generating.
    The parts, each saying one thing: race and body, Age, Look (the face), Mood (the expression), Role (who they
    are - from the animation name unless one is picked) and Outfit (what they wear - the role's own unless one
    is picked), with the creature's own colors put into the outfit."""
    race = str(row.get("race") or "").strip()
    anim = str(row.get("anim") or "").strip()
    sex = str(row.get("sex") or "").strip().lower()
    aw = set(re.findall(r"[a-z]+", anim.lower()))
    gender = "female" if aw & {"female", "woman", "girl"} else "male" if aw & {"male", "man", "boy"} else \
             "female" if sex == "female" else "male" if sex == "male" else ""   # the sprite's gender first
    races = {k: v for k, v in settings.get("race_terms", {}).items() if not k.startswith("_")}
    rkey = race.lower().replace("-", " ").replace("_", " ")
    rkey = re.sub(r"^half ?(orc|elf|ogre)$", r"half \1", rkey)      # RACE.IDS writes HALFORC, HALFELF
    _SPRITE_RACES = ("half elf", "half orc", "elf", "dwarf", "halfling", "gnome", "human")
    anim_race = next((k for k in _SPRITE_RACES if set(k.split()) <= aw), "")
    if not rkey or rkey.startswith("race "):                         # no usable race: look in the animation name
        rkey = anim_race
    elif rkey == "human" and anim_race and anim_race != "human":     # human is the race byte's default, so a sprite
        rkey = anim_race                                             # picked on purpose is the better evidence.
    elif anim_race and rkey not in _SPRITE_RACES and find_role(anim, settings) and \
            (_BEASTLY.search(rkey) or _UNDEAD.search(rkey) or rkey not in races):
        rkey = anim_race                                             # a "golem" walking about as a gnome cleric
    kind = next((k for k in races if k and k not in _SPRITE_RACES and set(k.split()) <= aw), "")
    if kind in _NAME_BLIND and find_role(anim, settings):          # a Shadow Thief is a man in black
        kind = ""
    if kind and _SUBRACE.get(kind) == rkey:                         # the "Drow Female" sprite on a file that says
        rkey = kind                                                  # ELF: the race field has no room for drow
    if kind and (rkey not in races or rkey in _VAGUE_RACES):         # a sirine's file says FAIRY and only its
        rkey = kind                                                  # sprite says sirine; the sprite wins there
    said_kind = str((row.get("colors") or {}).get("kind") or "")    # DrowSkin and the like: the surest of all
    if said_kind in races and said_kind != rkey and (not rkey or rkey in _VAGUE_RACES or rkey in _SPRITE_RACES
                                                     or _SUBRACE.get(said_kind) == rkey):
        rkey = said_kind                                             # a drow filed as an elf - but a goblin
        #                                                              wearing a kobold's colors is still a goblin
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
    spr0 = sprite(anim, settings)
    if spr0.get("race") and rkey not in _SPRITE_RACES:             # Mia the lycanthrope walks around as a human
        rkey = str(spr0["race"])                                     # girl: the sprite shows what she looks like
    child = bool(spr0.get("child")) or age == "Child"
    race_txt = races.get(rkey, "")
    if gender == "female":                                           # a woman of some races needs her own words:
        race_txt = str((settings.get("race_terms_female") or {}).get(rkey) or race_txt)   # see its _about
    beard_pick = str((settings.get("beard_terms") or {}).get(beard, "")) if beard else ""
    picks_ = (settings.get("race_picks") or {}).get(rkey)
    spr_said_ = str((sprite(anim, settings) or {}).get("text") or "").lower()   # what the sprite shows wins:
    if (sprite(anim, settings) or {}).get("skin_colors") and ((row.get("colors") or {}).get("skin")
                                                            or (row.get("colors") or {}).get("skin_any")) \
            and settings.get("use_creature_colors", True):           # its skin is the creature's own color (an
        spr_said_ += ", skin"                                        # ogre is red in one game file, sallow in
    #                                                                  another): not the race's own skin
    if isinstance(picks_, dict) and race_txt:                        # no pick for a part it already says                        # a drow's red eyes, a dwarf's braided beard:
        rng_ = random.Random(creature_seed(face_key(row)) ^ 0x5EED) if face_key(row) else random.Random()
        for slot_, pool_ in picks_.items():                          # one creature's own, the same every time
            if str(slot_).startswith("_") or not isinstance(pool_, dict):
                continue
            got_ = _weighted(rng_, pool_)                            # drawn either way, so the next slot is unmoved
            if slot_ == "beard" and (gender == "female" or beard_pick) or not got_ or slot_ in race_txt \
                    or re.search(rf"\b{re.escape(str(slot_))}\b", spr_said_):
                continue
            race_txt = f"{race_txt}, {got_}"
    for part_ in ("skin", "ears", "face"):                         # the sprite's skin, ears, face - not the race's
        if race_txt and re.search(rf"\b{part_}\b", spr_said_):    # usual ones (an orog is red, not orc-green,
            race_txt = re.sub(rf",\s*[^,]*\b{part_}\b[^,]*", "", race_txt)   # its ears on top of its head)
    if race_txt and (sprite(anim, settings) or {}).get("bald"):     # a bald sprite: the race's hair goes too
        race_txt = re.sub(r",\s*[^,]*\bhair\b[^,]*", "", race_txt)
    if beard_pick and gender != "female" and race_txt:              # picked by hand: with the race, said early
        race_txt = re.sub(r",\s*[^,]*\bbeard\b[^,]*", "", race_txt)   # instead of the race's own
        race_txt = f"{race_txt}, {beard_pick}"
    if not race_txt and rkey:                                        # a race nobody has written a line for: the
        race_txt = (f"{rkey}, with the head and face of a {rkey} and not of a person"   # model paints a human
                    if rkey not in _HUMANOID else rkey)              # unless it is told plainly not to
    if rkey and rkey not in _HUMANOID and rkey not in races and (not gender or _BEASTLY.search(rkey)):
        #                                                              a wolf, male or not, wears no tunic: its
        #                                                              file's colors are a sprite's, not clothes
        subject = anim.lower() or rkey                               # a wolf, a golem...: say what it is
        w_ = subject.split()
        if len(w_) > 1 and w_[0] == rkey:                           # "Wolf Winter" is a winter wolf
            subject = " ".join(w_[1:] + w_[:1])
        return subject, subject
    age_txt = "child" if child else (settings.get("age_terms") or {}).get(age, age.lower()) if age else \
        ("young" if aw & {"boy", "girl", "child"} else "")       # a grown person is what it paints anyway:
    #                                                            only an age that is not the default is worth saying
    subject = " ".join(x for x in (gender, race_txt.split(",")[0]) if x) or "person"
    if child:                                                        # a child, never "female": the face bank's
        subject = f'{race_txt.split(",")[0]} child'.strip()           # child folder says which
    lead, _, extra = age_txt.partition(",")                          # "old, lined face" -> "old male human, lined face"
    if child:                                                        # "female human" paints a grown woman's body:
        parts = [" ".join(x for x in (lead.strip(), race_txt) if x)]   # just a child; the start face is a girl's
        #                                                              or a boy's (faces/child/female, male)
    else:
        parts = [" ".join(x for x in (lead.strip(), gender, race_txt) if x) or "person"]   # a name and nothing else
    if extra.strip():
        parts.append(extra.strip())
    manlike = rkey in ("", "human", "elf", "half elf", "dwarf", "halfling", "gnome", "half orc", "tiefling",
                       "drow", "aasimar", "duergar", "genasi")
    col = dict(row.get("colors") or {}) if settings.get("use_creature_colors", True) else {}
    for slot_ in ("skin", "hair"):                               # a random set in the file (OgreSkin): the game
        any_ = [w for w in str(col.pop(slot_ + "_any", "") or "").split("|") if w]   # paints any of its colors;
        if any_ and not col.get(slot_):                          # one of them, the same for this creature always
            rng_c = random.Random(creature_seed(face_key(row)) ^ 0xC010) if face_key(row) else random.Random()
            col[slot_] = rng_c.choice(any_)
    spr = sprite(anim, settings)
    bald = bool(spr.get("bald"))                                 # the sprite is bald; the file does not know it
    spr_txt = str(spr.get("text") or "")
    if spr_txt and spr.get("front"):                             # what the body is goes with the race itself,
        head, _, tail = spr_txt.partition(":")                   # "very fat, bald male human: a huge round face"
        parts[0] = f"{head.strip()} {parts[0]}" + (f": {tail.strip()}" if tail.strip() else "")
    elif spr_txt:                                                # a helmet: with the race, so the hair under it
        parts[0] = f"{parts[0]}, {spr_txt}"                      # goes unsaid
    feat = features(face_key(row), settings, bool(col.get("hair")), bald) if manlike else ""
    if child and feat:                                              # a child has no battle scars or broken nose
        feat = ", ".join(c for c in feat.split(", ") if not re.search(
            r"\b(scar|burn|pockmark\w*|missing|notched|cauliflower|broken|crooked jaw|receding|balding|bald|graying)\b", c))
    if gender == "female" and feat:                                # a bald head under a peasant woman's headscarf
        feat = ", ".join(c for c in feat.split(", ")               # came out a bearded old man
                         if not re.search(r"\b(bald|balding|receding|widow's peak)\b", c))
    #                                                            a neat side parting on a gnoll helps nobody
    if feat:                                                     # the race says it already: a halfling's button
        said_r = race_txt.lower()                                # nose and curls, a dwarf's broad face - the
        covered = {"face": "face" in said_r, "nose": "nose" in said_r,   # face draw must not argue with them
                   "hair": bool(re.search(r"\b(hair|curls|braids|topknot|bald|beard|mane)\b", said_r))}
        terms_ = settings.get("feature_terms") or {}
        feat = ", ".join(c for c in feat.split(", ")
                         if not any(covered[k] and c in (terms_.get(k) or []) for k in covered))
    hair_said = False
    if col.get("hair"):
        feat = _HAIR_COLOR.sub("", feat)                        # the file says what color it is; the draw only
        at = re.search(r"\bhair\b", feat)                      # says what it does. One clause, not two: "short
        if at and not any(w in feat.lower() for w in _BALD):     # neat hair, silver-white hair" is the same hair
            adj = re.sub(r"\s*hair\s*$", "", str(col["hair"])).strip()
            if adj and "hair" not in adj:                         # "hair dyed green" stays a clause of its own
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
        own_ = slot == "skin" and bool((sprite(anim, settings) or {}).get("skin_colors"))   # a sprite colored
        if not col.get(slot) or slot in said or (covered and not own_):                     # by its file
            continue
        if slot == "hair" and (bald or any(w in feat.lower() for w in _BALD)     # no hair color on a bald head, nor
                               or any(w in said for w in _COVERED_HEAD)):   # under a helm the sprite never takes off
            continue
        parts.append(col[slot])
    spirit = any(w in race_txt.lower() for w in ("ghostly", "translucent", "mist", "darkness"))
    #                                                            nothing is wearing armor inside a spirit, and the
    #                                                            sprite's class says only which BAM was borrowed
    bare = {str(x).lower().replace("-", " ") for x in (settings.get("bare_races") or []) if not str(x).startswith("_")}
    beast = rkey in bare                                         # a sahuagin's class says which BAM it borrowed,
    #                                                              not that it is wearing a soldier's kit
    dressed = "outfit" in spr                                   # the sprite says what it wears
    role = "" if spirit or beast or (dressed and not spr.get("outfit")) else find_role(anim, settings)
    if role:
        parts.append(str(roles(settings)[role].get("who") or role.lower()))
    if feat and not (bald and "bald" in " ".join(parts[:3]).lower()):
        parts.append(feat)
    outfits = settings.get("outfit_terms") or {}
    picked = outfit if outfit in outfits and outfit != BY_ROLE else ""
    wear = picked or (str(spr.get("outfit") or "") if dressed else
                      str(roles(settings)[role].get("outfit") or "") if role else "")
    major, minor = (col.get("major", ""), col.get("minor", "")) if col else ("", "")
    if not wear and major and not (spirit or beast or dressed):  # no role, but the sprite has colored clothes:
        wear = "Plain"                                           # a bare color is painted as a modern shirt
    if wear and (picked or not (spirit or beast)):
        parts.append(outfit_text(settings, wear, gender, major, minor if minor != major else ""))
    if col.get("metal") and col["metal"] not in ("dark iron",):
        parts.append(col["metal"] + " buckles and clasps")       # "fittings" painted plate armor
    look_txt = (settings.get("look_terms") or {}).get(look, "") if look else ""
    if gender == "female" and look_txt:                      # the looks were written for men: a woman keeps
        look_txt = _no_facial_hair(look_txt)                 # the lines and the gray, not the beard
    mood_txt = str((settings.get("mood_terms") or {}).get(mood, "")) if mood else ""
    scar_txt = str((settings.get("scar_terms") or {}).get(scars or ("" if child else str(settings.get("scar_default") or "")), ""))
    grime_txt = str((settings.get("grime_terms") or {}).get(grime, "")) if grime else ""
    if not manlike and covered:                              # a gnoll's fur, a spectre's mist: no skin to mark
        scar_txt = grime_txt = ""
    for t in (mood_txt, grime_txt, scar_txt, look_txt):                           # right after the race, not at the end: Flux
        if t:                                                # weighs the front of a prompt hardest, and a scar
            parts.insert(1, t)                               # said last is mostly not painted at all
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


def save_background(settings: dict, style: str, background: str) -> None:
    """Keep an edited background, and nothing else, in settings.json: in the style when the style has a
    background of its own, otherwise the one every look shares."""
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
    except (OSError, ValueError) as e:
        raise GeneratorError(f"generator/settings.json could not be read: {e}") from e
    if not isinstance(raw, dict):
        raw = {}
    look = (settings.get("styles") or {}).get(style)
    if isinstance(look, dict) and look.get("background"):
        look["background"] = background
        rl = raw.setdefault("styles", {}).setdefault(style, {})
        if isinstance(rl, dict):
            rl["background"] = background
    else:
        raw["background"] = settings["background"] = background
    try:
        SETTINGS_FILE.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    except OSError as e:
        raise GeneratorError(f"generator/settings.json could not be written: {e}") from e


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
_DECK_LOCK = threading.Lock()
_FACE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def bank_root(settings: dict) -> Path:
    """The face bank folder: beside Portrait Scout unless the settings give a full path."""
    fb = settings.get("face_bank") or {}
    p = Path(str(fb.get("folder") or "faces"))
    return p if p.is_absolute() else Path(__file__).resolve().parent.parent / p


def bank_scale(settings: dict) -> float:
    """How big a bank face is painted, against the bank picture: below 1 for a little more head and shoulders."""
    try:
        return max(0.6, min(1.0, float((settings.get("face_bank") or {}).get("scale", 0.8))))
    except (TypeError, ValueError):
        return 0.8


def bank_strength(settings: dict) -> float:
    try:
        return max(0.05, min(1.0, float((settings.get("face_bank") or {}).get("strength", 0.68))))
    except (TypeError, ValueError):
        return 0.68


def bank_monster(settings: dict, text: str, race: str = "") -> Optional[Tuple[float, int, float]]:
    """(strength, steps, blur) for a monster with a face like a person's - an orc, ogre, goblin, hobgoblin,
    xvart - started from the bank: the face blurred, so its shape, set and light carry over and not a person's
    features, painted further and longer than a person's. None for anything else (a person, a beast, the
    undead, a kobold's snout)."""
    low = re.sub(r"\bnot (?:a |an |of a )?[\w-]+|\b[\w]+-like\b|\bpig snout\b|\bon (?:his|her|its|the) skull\b", "", str(text or "").lower())   # "not a kobold",
    #                                                                  "a snout-like nose": no kobold, no snout
    rc = str(race or "").lower()
    if not (_MONSTROUS.search(low) or _MONSTROUS.search(rc)):
        return None
    if _NOT_A_FACE.search(low) or _UNDEAD.search(low) or incorporeal(low):
        return None
    m = (settings.get("face_bank") or {}).get("monster") or {}
    if m.get("off"):
        return None
    hulk = bool(re.search(r"\bsmall head sunk\b|\bmassive hunched shoulders\b", low))
    try:
        return (max(0.3, min(1.0, float(m.get("hulk_strength" if hulk else "strength", 0.85 if hulk else 0.74)))),
                max(1, min(60, int(m.get("steps", 8)))), max(0.0, float(m.get("blur", 0))), hulk)
    except (TypeError, ValueError):
        return (0.85 if hulk else 0.74), 8, 0.0, hulk


_EDIT_SKIN = {"half-ogre": "sallow olive skin", "orc": "green skin", "orog": "dull reddish-orange skin", "hobgoblin": "dark reddish-brown skin", "goblin": "sallow yellow-green skin", "xvart": "bright blue skin", "githyanki": "jaundiced yellow skin"}


def monster_edit(settings: dict, text: str, race: str = "", anim: str = "") -> Optional[Tuple[str, float, int]]:
    """The second pass for a monster from the face bank: the first keeps the bank face (a person, but every one
    different), this one makes it the monster in words, keeping the face's own shape. Painted straight from the
    words, every orc or half-ogre comes out one face. (prompt, strength, steps), or None."""
    mon = bank_monster(settings, text, race)
    if not mon or mon[3]:
        return None
    m = (settings.get("face_bank") or {}).get("monster") or {}
    try:
        amt = float(m.get("edit", 0.85))
    except (TypeError, ValueError):
        amt = 0.85
    if amt <= 0:
        return None
    low = str(text or "").lower()
    kind = "half-ogre" if re.search(r"\bhalf.?ogre\b", (anim + " " + low).lower()) else \
        (_MONSTROUS.search(low) or _MONSTROUS.search(str(race or "").lower()))
    kind = kind if isinstance(kind, str) else kind.group(0) if kind else "monster"
    tpl = str((settings.get("monster_edits") or {}).get(kind) or "")
    if tpl:
        skin_ = re.search(r"[^,:]*\bskin\b[^,]*", low)
        sk_ = skin_.group(0).strip() if skin_ else _EDIT_SKIN.get(kind, "coarse skin")
        who_ = "woman" if re.search(r"\b(female|woman)\b", low) else "man"
        return (f"Make this {who_} {tpl.replace('{skin}', sk_)}. Paint the whole picture crisp and sharp, the neck, shoulders and clothes in full detail with no haze or smudge. Keep everything else the same: the same "
                f"person, {'her' if who_ == 'woman' else 'his'} own face shape and features, the clothing, helmet, "
                f"pose, lighting, background and painting style.", amt, max(1, int(m.get("steps", 8))))
    rk = "ogre" if kind == "half-ogre" else kind
    words = [w.strip() for w in str((settings.get("race_terms") or {}).get(rk) or "").split(":")[-1].split(",")[1:] if w.strip()]
    words = [w for w in words if not re.search(r"\b(not|human|close up|tiny head|gigantic)\b", w)][:4]
    skin = re.search(r"[^,]*\bskin\b[^,]*", low)
    if skin and not any("skin" in w for w in words):
        words.append(skin.group(0).split(":")[-1].strip())
    who = "woman" if re.search(r"\b(female|woman)\b", low) else "man"
    feats = ", ".join(words) or "a coarse brutish face"
    return (f"Make this {who} a coarse brutish {kind}: {feats}. Paint the whole picture crisp and sharp, the neck, shoulders and clothes in full detail with no haze or smudge. Keep everything else the same: the same "
            f"person, his own face shape and features, the clothing, helmet, pose, lighting, background and painting "
            f"style.".replace("his own", "her own" if who == "woman" else "his own"), amt, max(1, int(m.get("steps", 8))))


def bank_steps(settings: dict) -> int:
    try:
        return max(1, min(60, int((settings.get("face_bank") or {}).get("steps", 6))))
    except (TypeError, ValueError):
        return 6


_CHILDISH = re.compile(r"\b(nine years old|(?<!not )a (?:small )?child\b|(?<!not a )(?<!not a small )child\b|boy|girl|kid)\b")
#                                                                  "child male human" from Age, not "not a child"


_NOT_A_FACE = re.compile(r"\b(were\w*|lycanthrope|muzzle|snout|beak|tentacles?|mandibles|hyena|gnoll|lizard\w*|"
                         r"insect\w*|not of a person|skeleton|undead spirit|mind flayer|beholder|golem|elemental|"
                         r"slime|ooze|dragon|wyvern|animal|beast|kobold|ghoul|ghast|zombie|mummy|lich|revenant|"
                         r"wight|corpse|undead|spectre|specter|ghost|wraith)\b")
#   narrower than _BEASTLY: a fur-trimmed cloak or a raven brooch is still a person's face


def bank_fits(text: str, race: str = "") -> bool:
    """Whether a person's photo is a fair start: not for beasts, monsters, the undead or ghosts, whose faces the
    words have to make on their own."""
    low, rc = text.lower(), str(race or "").lower()
    if rc and not re.search(rf"\b{re.escape(rc)}\b", low):          # Mia the lycanthrope walks about as a human
        rc = ""                                                      # girl, and is described as one
    if rc and (_BEASTLY.search(rc) or _MONSTROUS.search(rc) or _UNDEAD.search(rc) or _NOT_A_FACE.search(rc)):
        return False
    if _BANK_LOOK.search(low):                                     # a photo's hair, shirt and clean chin win over
        return False                                                 # the words: a bald, bearded, bare-chested
    return not (_NOT_A_FACE.search(low) or _MONSTROUS.search(low) or incorporeal(low))   # beggar came out combed


_BANK_LOOK = re.compile(r"\b(?!x)x\b")                             # nothing for now: bald, beards, bare chests and
#                                                                  ponytails are painted in by bank_fill


def bank_pick(settings: dict, race: str = "", text: str = "") -> Optional[Path]:
    """The next face from the bank for this race: its own folder if there is one, else human (a child only from a
    child folder). Dealt like cards from a shuffled deck, so no face comes back until all have been used.
    None when nothing fits."""
    root = bank_root(settings)
    if not bank_fits(text, race) and not bank_monster(settings, text, race):
        return None
    child = bool(_CHILDISH.search(text.lower())) if text else False
    race_ = re.sub(r"[^a-z\-]", "", str(race or "").lower())
    names = ["child"] if child else [n for n in (race_, "human") if n]
    low = text.lower()
    sex = "female" if re.search(r"\b(female|woman|girl|lady)\b", low) else \
          "male" if re.search(r"\b(male|man|boy)\b", low) else random.choice(("male", "female"))
    skip_ = {str(x).lower() for x in ((settings.get("face_bank") or {}).get("skip") or [])}
    if race_ in skip_ and not (root / race_).is_dir():
        return None
    for nm in dict.fromkeys(names):
        d = root / nm
        if (d / sex).is_dir():                                       # a face of the picture's own gender: the
            d, nm = d / sex, f"{nm}_{sex}"                           # start face's gender is the one painted
        if not d.is_dir():
            continue
        with _DECK_LOCK:
            deck_f = root / f"deck_{nm}.json"
            try:
                deck = [str(x) for x in json.loads(deck_f.read_text(encoding="utf-8"))]
            except (OSError, ValueError, TypeError):
                deck = []
            for _ in range(2):
                if not deck:                                         # all dealt (or a new bank): shuffle again
                    deck = [f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in _FACE_EXT]
                    random.shuffle(deck)
                while deck:
                    name = deck.pop()
                    if (d / name).is_file():
                        try:
                            deck_f.write_text(json.dumps(deck), encoding="utf-8")
                        except OSError:
                            pass
                        return d / name
    return None


def edit_prompt(settings: dict, expression: str = "", change: str = "") -> str:
    """The words for an edit of a finished picture: its expression changed (a name from edit_terms'
    expressions, or words), or any change in words, with everything else kept."""
    et = settings.get("edit_terms") or {}
    keep = str(et.get("keep") or "Keep everything else exactly the same.")
    if expression:
        words = str((et.get("expressions") or {}).get(expression) or expression)
        return f"Change the person's facial expression to {words}. {keep}"
    change = change.strip().rstrip(".")
    return f"{change[:1].upper()}{change[1:]}. {keep}"


def bank_bald(text: str) -> bool:
    """Whether the words ask for a bald or shaven head: bank_fill then paints over the photo's hair."""
    return bool(re.search(r"\b(bald|shaven head|hairless head|bare scalp)\b", str(text or "").lower()))


def bank_wide(text: str) -> float:
    """How much wider a bank face is made for the words: a fat man's from a thin man's photo."""
    low = str(text or "").lower()
    return 1.35 if re.search(r"\b(very fat|obese|enormously fat|grossly fat)\b", low) else 1.0   # plump or a
    #                                                                  double chin is left to the words: covering the
    #                                                                  photo's cheeks covers who it is (halflings all
    #                                                                  came out one face)


def bank_bare(text: str) -> bool:
    """Whether the words ask for a bare chest or bare shoulders: bank_fill then paints skin below the chin."""
    return bool(re.search(r"\b(bare-chested|shirtless|no shirt|bare chest|shoulders bare|bare shoulders)\b",
                          str(text or "").lower()))


def bank_nose(text: str) -> float:
    """How much bigger a bank face's nose is made for the words: a gnome's long, pointed or hooked one."""
    low = str(text or "").lower()
    if not re.search(r"\bgnomes?\b", low):                           # a gnome's only: a human's "long, thin nose"
        return 1.0                                                   # from the face words is the photo's to keep
    return 1.5 if re.search(r"\b(very large|very long|long|hooked|crooked|pointed)\b[^,]{0,15}\bnose\b",
                            low) else 1.0


def bank_ears(text: str) -> bool:
    """Whether the words ask for big pointed ears (a gnome's): bank_fill then paints them on."""
    if not re.search(r"\bgnomes?\b", str(text or "").lower()):       # a gnome's only: an elf's or a drow's come
        return False                                                 # out of the words well enough
    return bool(re.search(r"\b(big|large|long)\b[^,]{0,10}\bears\b[^,]{0,20}\b(point|points|pointed)\b|"
                          r"\b(big|large|long)\b[^,]{0,25}\bpointed ears\b", str(text or "").lower()))


def bank_beard(text: str) -> bool:
    """Whether the words ask for a long beard (a dwarf's, a wizard's): bank_fill then paints over the photo's
    chin and neck, which would otherwise keep a short one."""
    return bool(re.search(r"\b(long|very long|braided|forked|chest|waist)\b[^,]*\bbeard\b|\bbeard\b[^,]*\b(chest|waist)\b",
                          str(text or "").lower()))


def bank_body(settings: dict, face: str = "") -> Optional[bytes]:
    """A painted head-and-shoulders picture from faces/_bodies for bank_fill, of the face's gender when the
    face's path says it (male_*.png, female_*.png), any of them otherwise; None when there are none."""
    d = bank_root(settings) / "_bodies"
    parts = {x.lower() for x in Path(face).parts}
    sex = "female" if "female" in parts else "male" if "male" in parts else ""
    try:
        got = [f for f in d.iterdir() if f.suffix.lower() in _FACE_EXT and (not sex or f.stem.startswith(sex + "_"))]
        return random.choice(got).read_bytes() if got else None
    except OSError:
        return None


def bank_fill(data: bytes, body: Optional[bytes] = None, scale: float = 1.0, beard: bool = False,
              bald: bool = False, wide: float = 1.0, bare: bool = False, nose: float = 1.0,
              ears: bool = False, blur: float = 0.0, chin: bool = False) -> bytes:
    """A bank face with the blurred padding around it painted over. The faces were cut from photos that end at
    the chin, so the bottom of each and often its sides are a smooth blur - and a picture started from a blur
    keeps it: the bottom half washed out, or the face in a soft circle. The blur (smooth areas reaching the
    edge) gets a dark shoulders-and-neck shape in the collar's color and brush-sized light and dark over a
    blurred copy of the picture, which the painting turns into clothes and background. With body (a painted
    head-and-shoulders picture, faces/_bodies), its light and dark - folds, straps, a hood's edge - go over
    that shape instead: real painted shapes come back as clothes, where a soft fill came back as blur and a
    made-up texture as blocks."""
    try:
        import io
        from PIL import Image, ImageChops, ImageDraw, ImageFilter
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return data
    w, h = im.size
    if chin:                                                         # a child: a bigger head on a smaller body, and
        scale = max(scale, 0.9)                                      # a boy's plain tunic folds - a woman's bodice
        body = None                                                  # painting gave girls a grown woman's figure
        try:
            bd_ = sorted((Path(__file__).resolve().parent.parent / "faces" / "_bodies").glob("male_*.png"))
            if bd_:
                body = random.choice(bd_).read_bytes()
        except OSError:
            body = None
    sw, sh = max(3, w // 8), max(3, h // 8)
    sm = im.convert("L").filter(ImageFilter.FIND_EDGES).resize((sw, sh), Image.BOX).point(lambda v: 255 if v < 2 else 0)
    sm = sm.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    for box in ((0, 0, sw, 1), (0, sh - 1, sw, sh), (0, 0, 1, sh), (sw - 1, 0, sw, sh)):
        sm.paste(255, box)                                           # the edge filter's own border is not detail
    px = sm.load()
    m = Image.new("L", (sw, sh), 0)
    mp = m.load()
    todo = [(x, y) for x in range(1, sw - 1) for y in (1, sh - 2)] + [(x, y) for y in range(1, sh - 1) for x in (1, sw - 2)]
    while todo:                                                      # smooth blocks joined to the edge
        x, y = todo.pop()
        if not (0 <= x < sw and 0 <= y < sh) or mp[x, y] or not px[x, y]:
            continue
        mp[x, y] = 255
        todo += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    if not any(m.getextrema()) and scale >= 1.0:
        return data
    if 0.5 < scale < 1.0:                                            # the face smaller, its eyes where they were:
        at = ((w - round(w * scale)) // 2, round(0.335 * h * (1 - scale)))   # the bank's faces are a little
        size = (round(w * scale), round(h * scale))                  # bigger than a painted head and shoulders
        canvas = im.filter(ImageFilter.GaussianBlur(40))
        canvas.paste(im.resize(size, Image.LANCZOS), at)
        im = canvas
        mm = Image.new("L", (w, h), 255)                             # the blur found before shrinking, shrunk with
        inner = m.resize((w, h), Image.NEAREST).resize(size, Image.NEAREST)        # it: everything around is fill,
        ImageDraw.Draw(inner).rectangle((0, 0, size[0] - 1, size[1] - 1), outline=255, width=max(8, w // 24))
        mm.paste(inner, at)                                          # and its own edge too: no box around the head
        m = mm.resize((sw, sh), Image.BOX).point(lambda v: 255 if v > 127 else 0)
        mp = m.load()
    yb = next((y for y in range(sh - 1, -1, -1) if sum(1 for x in range(sw) if not mp[x, y]) > sw * 0.25), sh - 1) * 8
    eye_y = 0.335 * h                                                # where the bank puts the eyes
    hair = im.crop((int(0.42 * w), max(0, int(eye_y - 0.42 * w * scale)), int(0.58 * w),
                    max(1, int(eye_y - 0.35 * w * scale)))).resize((1, 1), Image.BOX).getpixel((0, 0))   # the crown
    if beard or bare or chin:                                        # a long beard: the photo's chin and neck go
        ym = int(eye_y + (0.2 if beard else 0.32 if chin else 0.34) * w * scale)   # too, from the mouth down, or the painting
        #                                                              (chin: a child's photo - its bright T-shirt
        #                                                              came back as a pale band under the chin)
        #                                                              (a bare chest: from under the chin, or the
        #                                                              photo's collar stays)
        m.paste(255, (0, ym // 8, sw, sh))                           # keeps a short one
        mp = m.load()
        yb = min(yb, ym + 40)
    m = m.filter(ImageFilter.MaxFilter(5))                           # over the photo's hard edge, not up to it
    mp = m.load()
    base = im.filter(ImageFilter.GaussianBlur(30))
    back = (im.crop((0, 0, w, int(eye_y))).resize((w, h), Image.BILINEAR) if chin else im).filter(
        ImageFilter.GaussianBlur(80 if chin else 70))                # the background: a child's from above its
    #                                                                  shoulders only - its photo's pale shirt
    #                                                                  smeared beside the narrow shoulders
    try:                                                             # with a painted backdrop's light and brush
        from PIL import ImageOps                                     # strokes on it: a plain blur is painted back
        bds_ = sorted((Path(__file__).resolve().parent.parent / "faces" / "_backdrops").glob("*.png"))   # as haze
        if bds_:
            bl_ = ImageOps.autocontrast(Image.open(random.choice(bds_)).convert("L").resize((w, h), Image.LANCZOS))
            bl_ = Image.blend(Image.new("L", (w, h), 128), bl_, 0.55)
            back = ImageChops.add(back, Image.merge("RGB", (bl_, bl_, bl_)), 1.0, -128)
    except Exception:  # noqa: BLE001 - no backdrops: the plain blur
        pass
    #                                                                  a photo's pale shirt blurred in came back as
    #                                                                  a washed-out smear beside the shoulders
    sc = im.crop((w // 3, max(0, yb - 40), 2 * w // 3, max(1, yb - 5))).resize((1, 1), Image.BOX).getpixel((0, 0))
    sc = tuple(int(v * 0.6) for v in sc)                             # the collar's color, darker
    if bare:                                                         # a bare chest: skin, from the chin down
        sc = im.crop((int(0.46 * w), int(eye_y + 0.06 * w * scale), int(0.54 * w), int(eye_y + 0.13 * w * scale))
                     ).resize((1, 1), Image.BOX).getpixel((0, 0))
        body = None
    d = ImageDraw.Draw(base)
    sx0, sx1 = (0.0, 1.0) if chin else (-0.05, 1.05)                # a child: to the edges too (narrow ones came back blurred)
    d.ellipse((int(sx0 * w), yb - 10, int(sx1 * w), yb + int(1.2 * h)), fill=sc)
    d.rectangle((int(0.35 * w), yb - 60, int(0.65 * w), yb + 20), fill=sc)
    base = base.filter(ImageFilter.GaussianBlur(12))
    shape = Image.new("L", (w, h), 0)                                # where the shoulders go: the body picture's
    ds = ImageDraw.Draw(shape)                                       # folds there only - its arches and walls
    ds.ellipse((int(sx0 * w), yb - 10, int(sx1 * w), yb + int(1.2 * h)), fill=255)   # behind would come
    ds.rectangle((int(0.35 * w), yb - 60, int(0.65 * w), yb + 20), fill=255)          # back as a background
    shape = shape.filter(ImageFilter.GaussianBlur(16))
    filled = None
    if body:
        try:
            from PIL import ImageOps
            lum = ImageOps.autocontrast(Image.open(io.BytesIO(body)).convert("L").resize((w, h), Image.LANCZOS))
            lum = lum.point(lambda v: min(255, int(v * 1.6)))
            lum = Image.blend(lum, Image.new("L", (w, h), 230), 0.35)   # folds, not a whole outfit to copy
            filled = Image.composite(ImageChops.multiply(base, Image.merge("RGB", (lum, lum, lum))), back, shape)
        except Exception:  # noqa: BLE001
            filled = None
    if filled is None:
        lum = Image.effect_noise((w // 40 + 2, h // 40 + 2), 40).resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(14))
        nz = Image.blend(Image.new("RGB", (w, h), (128, 128, 128)), Image.merge("RGB", (lum, lum, lum)), 0.6)
        filled = Image.composite(ImageChops.add(base, nz, 1.0, -128), back, shape)
    if beard:                                                        # a beard down the chest, in the hair's color,
        bw, by = int(0.3 * w * scale), int(eye_y + 0.16 * w * scale)  # combed in long strands
        bm = Image.new("L", (w, h), 0)
        ImageDraw.Draw(bm).ellipse((w // 2 - bw, by, w // 2 + bw, by + int(0.45 * h)), fill=255)
        bm = bm.filter(ImageFilter.GaussianBlur(14))
        strands = Image.effect_noise((w // 5 + 1, h // 60 + 1), 22).resize((w, h), Image.BICUBIC)
        col = Image.new("RGB", (w, h), tuple(int(v * 0.75) for v in hair))
        col = ImageChops.add(col, Image.merge("RGB", (strands, strands, strands)), 1.0, -128)
        filled = Image.composite(col, filled, bm)
    mask = m.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(24))
    out = Image.composite(filled, im, mask)
    s_ = w * scale
    skin_ = im.crop((int(0.46 * w), int(eye_y + 0.06 * s_), int(0.54 * w), int(eye_y + 0.13 * s_))
                    ).resize((1, 1), Image.BOX).getpixel((0, 0))
    if wide > 1.0:                                                   # a fat face: full cheeks, jowls and a double
        fw = 0.27 * s_ * wide                                        # chin in the skin's color round the photo's
        fat = Image.new("L", (w, h), 0)                              # own eyes, nose and mouth
        ImageDraw.Draw(fat).ellipse((int(w / 2 - fw), int(eye_y - 0.02 * s_), int(w / 2 + fw), int(eye_y + 0.52 * s_)), fill=110)
        keep = Image.new("L", (w, h), 0)
        ImageDraw.Draw(keep).ellipse((int(w / 2 - 0.25 * s_), int(eye_y - 0.16 * s_), int(w / 2 + 0.25 * s_), int(eye_y + 0.36 * s_)), fill=255)
        fat = ImageChops.subtract(fat.filter(ImageFilter.GaussianBlur(12)), keep.filter(ImageFilter.GaussianBlur(16)))
        jowl = Image.new("RGB", (w, h), skin_)
        sh_ = Image.radial_gradient("L").resize((int(2 * fw), int(0.7 * s_)))
        lay = Image.new("L", (w, h), 0)
        lay.paste(sh_, (int(w / 2 - fw), int(eye_y - 0.05 * s_)))
        jowl = ImageChops.subtract(jowl, Image.merge("RGB", (lay, lay, lay)).point(lambda v: v // 5))
        out = Image.composite(jowl, out, fat)
    if nose > 1.0:                                                   # a gnome's long nose: the photo's own, made
        nb = (int(w / 2 - 0.09 * s_), int(eye_y + 0.02 * s_), int(w / 2 + 0.09 * s_), int(eye_y + 0.25 * s_))
        part = out.crop(nb)                                          # bigger, hung from the same bridge
        pw, ph = int(part.width * (1 + (nose - 1) * 0.6)), int(part.height * nose)
        part = part.resize((pw, ph), Image.LANCZOS)
        pm = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(pm).ellipse((0, 0, pw - 1, ph - 1), fill=255)
        out.paste(part, (w // 2 - pw // 2, nb[1]), pm.filter(ImageFilter.GaussianBlur(max(3, pw // 10))))
    if ears:                                                         # big ears tapering to points, out and up
        em = Image.new("L", (w, h), 0)
        de = ImageDraw.Draw(em)
        for sx in (-1, 1):
            x0 = w / 2 + sx * 0.27 * s_
            de.polygon([(x0, eye_y - 0.04 * s_), (x0 + sx * 0.06 * s_, eye_y + 0.18 * s_),
                        (x0 + sx * 0.2 * s_, eye_y - 0.24 * s_)], fill=255)
        em = em.filter(ImageFilter.GaussianBlur(5))
        earc = Image.new("RGB", (w, h), tuple(int(v * 0.9) for v in skin_))
        out = Image.composite(earc, out, em)
    if bald:                                                         # the photo's hair painted over with a bare
        skin = skin_                                                 # scalp in its own skin color, or the hair wins
        hw = 0.29 * s_ * min(wide, 1.15)
        dome = Image.new("L", (w, h), 0)
        ImageDraw.Draw(dome).ellipse((int(w / 2 - hw), int(eye_y - 0.43 * s_), int(w / 2 + hw), int(eye_y + 0.05 * s_)), fill=255)
        dome = ImageChops.multiply(dome, Image.linear_gradient("L").resize((w, h)).point(   # down to the brow
            lambda v, top=int(255 * (eye_y - 0.1 * s_) / h): 255 if v < top else max(0, 255 - (v - top) * 12)))
        dome = dome.filter(ImageFilter.GaussianBlur(10))
        shade = Image.radial_gradient("L").resize((int(2 * hw), int(0.6 * s_)))    # light on top, darker round
        scalp = Image.new("RGB", (w, h), skin)
        sh_full = Image.new("L", (w, h), 128)
        sh_full.paste(shade, (int(w / 2 - hw), int(eye_y - 0.46 * s_)))
        scalp = ImageChops.subtract(scalp, Image.merge("RGB", (sh_full, sh_full, sh_full)).point(lambda v: v // 4), 1, 16)
        hair_ = ImageChops.difference(out, Image.new("RGB", (w, h), skin)).convert("L").point(   # only where there
            lambda v: 255 if v > 38 else 0).filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(8))   # is
        dome = ImageChops.multiply(dome, hair_)                      # hair: a head already bald keeps its own scalp
        #                                                              (painted over, it came back a blurred patch)
        fh_ = out.crop((int(w / 2 - 0.1 * s_), int(eye_y - 0.2 * s_), int(w / 2 + 0.1 * s_), int(eye_y - 0.08 * s_)))
        tex_ = fh_.convert("L").resize((max(1, fh_.width // 2), max(1, fh_.height // 2))).resize(   # the forehead's own
            (w, h), Image.BICUBIC)                                   # skin texture on it, not a flat patch
        tex_ = Image.blend(Image.new("L", (w, h), 128), tex_.point(lambda v, m=int(sum(fh_.convert("L").getdata()) /
                                                                              max(1, fh_.width * fh_.height)): 128 + (v - m)), 0.8)
        scalp = ImageChops.add(scalp, Image.merge("RGB", (tex_, tex_, tex_)), 1.0, -128)
        out = Image.composite(scalp, out, dome)
    if blur > 0:                                                     # a monster's: the face's shape and light,
        out = out.filter(ImageFilter.GaussianBlur(blur))             # not a person's features, and a sallow,
        olive_ = ImageChops.multiply(out, Image.new("RGB", (w, h), (150, 150, 95))).point(lambda v: min(255, int(v * 1.5)))
        out = Image.blend(out, olive_, 0.6)                          # coarser cast: the second pass takes it further
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def bank_hulk(data: bytes, text: str = "", rnd: Optional[random.Random] = None) -> bytes:
    """A start picture for a hulking brute (an ogre): the bank face small and high, a huge body below it in the
    face's skin color - wider or narrower, a bigger or smaller belly, a vest, robes or nothing as the words
    say - so the painting comes out a small head sunk between massive shoulders, every one built a little
    differently and with a face of its own."""
    try:
        import io
        from PIL import Image, ImageDraw, ImageFilter
        face = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return data
    rnd = rnd or random.Random()
    low = str(text or "").lower()
    w, h = face.size
    skin = face.crop((int(.46 * w), int(.36 * h), int(.54 * w), int(.42 * h))).resize((1, 1), Image.BOX).getpixel((0, 0))
    c = face.filter(ImageFilter.GaussianBlur(80))
    dr = ImageDraw.Draw(c)
    wide = rnd.uniform(0.25, 0.45)                                   # how far past the frame the shoulders go
    top = rnd.uniform(0.17, 0.24)                                    # how high they hunch
    dr.ellipse((int(-wide * w), int(top * h), int((1 + wide) * w), int(1.5 * h)), fill=tuple(int(v * 0.85) for v in skin))
    belly = rnd.uniform(0.0, 0.12)
    dr.ellipse((int((0.05 - belly) * w), int(0.6 * h), int((0.95 + belly) * w), int(1.25 * h)),
               fill=tuple(int(v * 0.92) for v in skin))
    if "robe" in low:                                                # an ogre mage's robes, pale, over it all
        x0 = rnd.uniform(0.08, 0.2)
        dr.rectangle((int(x0 * w), int((top + 0.12) * h), int((1 - x0) * w), h), fill=(150, 140, 130))
    elif "bare chest" not in low or "vest" in low:                   # a vest down the front
        x0 = rnd.uniform(0.18, 0.26)
        dr.rectangle((int(x0 * w), int(rnd.uniform(0.5, 0.58) * h), int((1 - x0) * w), h), fill=(70, 48, 32))
    c = c.filter(ImageFilter.GaussianBlur(14))
    sc = rnd.uniform(0.38, 0.46)                                     # the head: small, high, its own
    small = face.resize((int(w * sc), int(h * sc)), Image.LANCZOS)
    m = Image.new("L", small.size, 0)
    ImageDraw.Draw(m).ellipse((int(.2 * small.width), int(.05 * small.height), int(.8 * small.width), int(.62 * small.height)), fill=255)
    at = ((w - small.width) // 2 + int(rnd.uniform(-0.03, 0.03) * w), int((top + 0.02) * h - 0.335 * h * sc))
    c.paste(small.filter(ImageFilter.GaussianBlur(5)), at, m.filter(ImageFilter.GaussianBlur(8)))
    buf = io.BytesIO()
    c.save(buf, "PNG")
    return buf.getvalue()


def fit_init(data: bytes, width: int, height: int) -> bytes:
    """A start picture made the size being painted (cut to the same shape, not stretched), as PNG. A portrait
    from the game is far smaller and a little narrower; the bank's faces are already the right size."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001 - no PIL, or not a picture: send it as it is
        return data
    if im.size == (width, height):
        return data
    k = max(width / im.width, height / im.height)
    im = im.resize((max(width, round(im.width * k)), max(height, round(im.height * k))), Image.LANCZOS)
    x, y = (im.width - width) // 2, (im.height - height) // 4
    im = im.crop((x, y, x + width, y + height))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


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
        edir = root / ("engine-neo" if "neo" in cmd.lower() else "engine")
        mark = edir / ".portrait_scout_started"                      # a first start installs Forge's packages
        first_start = edir.is_dir() and not mark.exists()
        self.log("Starting the image engine for the first time: Forge installs its packages and sets itself up. "
                 "This can take 10 to 30 minutes, once - later starts take about a minute..." if first_start else
                 "Starting the image engine (loading the model can take a minute or two)...")
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
        deadline = began + max(float(self.e.get("start_timeout_seconds", 600)), 3600 if first_start else 0)
        while time.time() < deadline:
            if self.alive():
                self.log("Image engine is ready.")
                if first_start:
                    try:
                        mark.write_text("Portrait Scout started this engine once; later starts are quick.\n",
                                        encoding="utf-8")
                    except OSError:
                        pass
                return
            if self.proc.poll() is not None:
                raise GeneratorError("The image engine stopped while starting. See generator/engine.log for why.")
            last = ""
            try:                                                     # what the engine is printing right now
                tail = (root / "generator" / "engine.log").read_text(encoding="utf-8", errors="replace")[-600:]
                last = next((x.strip() for x in reversed(tail.splitlines()) if x.strip()), "")[:90]
            except OSError:
                pass
            took = int(time.time() - began)
            self.progress(None, (f"First start, setting up (10-30 min, only this once)... {took // 60}:{took % 60:02d}"
                                 if first_start else f"Starting the image engine... {took} s")
                          + (f"   [{last}]" if last else ""))
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
                 seed: Optional[int] = None, init: Optional[bytes] = None,
                 strength: float = 0.77, steps: Optional[int] = None,
                 then_edit: Optional[Tuple[str, float, int]] = None) -> List[bytes]:
        """`count` candidate images (PNG bytes), one request each so they can be shown as they arrive. With `init`
        (an image's bytes) each one starts from that picture instead of from noise (img2img), `strength` being how
        far it may move away from it."""
        self.start()
        self.canceled.clear()
        payload = {k: v for k, v in self.s.get("payload", {}).items() if not k.startswith("_")}
        payload.update(prompt=prompt, negative_prompt=negative, n_iter=1)
        route = "/sdapi/v1/txt2img"
        if init:
            payload.update(init_images=[base64.b64encode(fit_init(init, int(payload.get("width") or 656),
                                                                   int(payload.get("height") or 1024))).decode("ascii")],
                           denoising_strength=max(0.05, min(1.0, float(strength))), resize_mode=0)
            route = "/sdapi/v1/img2img"
            if steps:
                payload["steps"] = int(steps)
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
                    if step:                                         # Forge reports a token 1% while it loads
                        self.progress((i + frac) / count, f"Candidate {i + 1} of {count}: step {step} of {steps}" if steps
                                      else f"Candidate {i + 1} of {count}: {int(frac * 100)}%")
                    else:
                        took = int(time.time() - began)
                        self.progress(i / count, f"Candidate {i + 1} of {count}: loading the model... {took} s"
                                      + (" (the first load reads the whole model from disk; later ones are quicker)"
                                         if took > 45 else ""))
            threading.Thread(target=watch, daemon=True).start()
            try:
                res = self._call(route, payload, timeout=float(self.e.get("image_timeout_seconds", 900)))
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
            if then_edit and not self.canceled.is_set():            # a second pass, told in words what to change
                ep_ = {k: v for k, v in payload.items() if k not in ("init_images", "denoising_strength", "steps")}
                ep_.update(prompt=then_edit[0], init_images=[base64.b64encode(data).decode("ascii")],
                           denoising_strength=float(then_edit[1]), steps=int(then_edit[2]), resize_mode=0)
                self.progress((i + 0.9) / count, f"Candidate {i + 1} of {count}: second pass...")
                try:
                    r2_ = self._call("/sdapi/v1/img2img", ep_, timeout=float(self.e.get("image_timeout_seconds", 900)))
                    im2_ = r2_.get("images") or []
                    if im2_:
                        data = base64.b64decode(im2_[0].split(",", 1)[-1])
                except (urllib.error.URLError, OSError, ValueError):
                    pass                                             # the first pass stands
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
PAINT_TINTS = {                          # face paint: woad, ochre, ash - what a tribe has to hand
    "Woad blue": (44, 72, 142), "Red ochre": (152, 52, 32), "Black": (26, 23, 23), "White": (226, 220, 204),
    "Green": (52, 98, 52), "Yellow ochre": (192, 150, 62), "Ash": (150, 146, 140),
}
PAINT_STYLES = ("None", "Cheek stripes", "Eye band", "Slash", "Center line", "Dots", "Chin lines")
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


def _scar_path(cx: float, cy: float, ang: float, half: float, rnd: object, bend: float = 0.10,
               rough: float = 0.025, n: int = 28) -> Tuple[List[Tuple[float, float, float]], Tuple[float, float]]:
    """The line a scar or a cut follows: never ruler-straight - a slight bow one way, and a little wander along
    it, the way a blade drags over a cheekbone. Points are (x, y, t) with t running -1 .. 1 along it."""
    import math
    dx, dy = math.cos(ang), math.sin(ang)
    nx, ny = -dy, dx
    k = (1 if rnd.random() < 0.5 else -1) * bend * rnd.uniform(0.5, 1.0)        # type: ignore[attr-defined]
    pts, wob = [], 0.0
    for i in range(n + 1):
        t = -1.0 + 2.0 * i / n
        wob = wob * 0.6 + rnd.uniform(-1.0, 1.0) * rough                          # type: ignore[attr-defined]
        off = (k * (1.0 - t * t) + wob) * half
        pts.append((cx + dx * t * half + nx * off, cy + dy * t * half + ny * off, t))
    return pts, (nx, ny)


def _taper(draw: object, pts: List[Tuple[float, float, float]], width: float, fill: int = 255,
           shift: Tuple[float, float] = (0.0, 0.0), ends: float = 0.25) -> None:
    """Draw along a path, full width in the middle and thinning to the ends."""
    sx, sy = shift
    for (x0, y0, t0), (x1, y1, t1) in zip(pts, pts[1:]):
        tm = (t0 + t1) / 2
        wd = max(1.0, width * (ends + (1.0 - ends) * max(0.0, 1.0 - tm * tm) ** 0.5))
        draw.line([x0 + sx, y0 + sy, x1 + sx, y1 + sy], fill=fill, width=max(1, int(round(wd))))   # type: ignore[attr-defined]
        r = wd / 2
        draw.ellipse([x1 + sx - r, y1 + sy - r, x1 + sx + r, y1 + sy + r], fill=fill)            # type: ignore[attr-defined]


def _skin_near(img: object, cx: float, cy: float, rad: float) -> Tuple[int, int, int]:
    """The skin's color around a point: the lighter part of what is there, so a brow, a lash or a shadow the
    point happens to land on does not decide it."""
    w, h = img.size                                                                   # type: ignore[attr-defined]
    r = max(3, int(rad))
    box = (int(max(0, cx - r)), int(max(0, cy - r)), int(min(w, cx + r + 1)), int(min(h, cy + r + 1)))
    px = list(img.crop(box).getdata()) or [(150, 120, 100)]                           # type: ignore[attr-defined]
    px.sort(key=lambda c: c[0] * 3 + c[1] * 6 + c[2])
    mid = px[len(px) // 2: max(len(px) // 2 + 1, len(px) * 85 // 100)]                # the lighter half, not the glare
    return tuple(int(sum(c[i] for c in mid) / len(mid)) for i in range(3))            # type: ignore[return-value]


def _mix(a: Tuple[int, int, int], b: Tuple[int, int, int], k: float) -> Tuple[int, int, int]:
    return tuple(int(round(x + (y - x) * k)) for x, y in zip(a, b))                  # type: ignore[return-value]


def touch_up(data: bytes, hue: float = 110, amount: float = 0.0, sat: float = 1.3, catch: float = 0.6,
             light: float = 0.0, lip_hue: float = 6, lip_amount: float = 0.0, lip_sat: float = 1.7,
             spots: Optional[List[Tuple[float, float]]] = None, eye_hue: float = 35, eye_amount: float = 0.0,
             eye_sat: float = 185, eye_size: float = 0.035, eye_val: float = -1, grime: float = 0.0,
             creases: float = 0.0, scars: float = 0.0,
             scar_points: Optional[List[Tuple[float, float]]] = None, wounds: float = 0.0,
             reach: float = 0.17, ridge: float = 0.5, blood: str = "Blood", line: float = 1.0,
             angle: Optional[float] = None, edge: float = 0.5,
             speckles: float = 0.0, speckle_points: Optional[List[Tuple[float, float]]] = None,
             speckle_size: float = 0.16, speckle_depth: float = 0.6,   # -1 lighter than the skin .. +1 darker
             speckle_amount: float = 0.75,                       # and how strongly they are laid on
             glow: str = "None", glow_tint: str = "Fire", glow_amount: float = 0.0,
             glow_size: float = 0.035, glow_spill: float = 0.5, glow_round: float = 0.42,
             soot: float = 0.0, mud: float = 0.0, spatter: float = 0.0, sweat: float = 0.0,
             dirt_size: float = 1.0, paint: str = "None", paint_color: str = "Woad blue",
             paint_amount: float = 0.0, paint_size: float = 1.0,
             paint_eyes: Optional[List[Tuple[float, float]]] = None) -> bytes:
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
            and not lights and soot <= 0 and mud <= 0 and spatter <= 0 and sweat <= 0
            and not (paint != "None" and paint_amount > 0)):
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
                # A click in the middle of an eye lands on the pupil, which is black: matching that took the
                # pupil and the lashes and left the iris alone. So the iris is looked for around the click too -
                # the colored, middling-bright pixels - and used whenever the middle is that dark.
                pr_ = max(2, int(rad * 0.6))
                near_ = im.crop((int(cx - pr_), int(cy - pr_), int(cx + pr_ + 1), int(cy + pr_ + 1))).convert("HSV")
                col_ = [c for c in near_.getdata() if c[1] >= 45 and 40 <= c[2] <= 235]
                if vs < 45 and len(col_) >= 8:
                    import math as _m
                    ang_ = [c[0] / 256 * 2 * _m.pi for c in col_]   # hue is a circle: average it as one
                    hs = (_m.degrees(_m.atan2(sum(map(_m.sin, ang_)), sum(map(_m.cos, ang_)))) % 360) / 360 * 256
                    ss = sorted(c[1] for c in col_)[len(col_) // 2]
                    vs = sorted(c[2] for c in col_)[len(col_) // 2]
                near_v = vc.point(lambda x: 255 if abs(x - vs) <= 62 else 0)
                near_h = hc.point(lambda x: 255 if abs(((x - hs + 128) % 256) - 128) <= 40 else 0)
                gray_ = sc.point(lambda x: 255 if x < 36 else 0)     # a gray iris has no hue worth matching
                if ss >= 60:                                     # a colored iris is far more colored than the
                    near_h = ImageChops.multiply(near_h, sc.point(lambda x: 255 if x >= ss * 0.6 else 0))
                #                                                  lid beside it, so a pale warm lid drops out
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
            # A healed scar, the way the game's own portraits paint one: a pale, slightly pink line, thinning to
            # nothing at both ends, bowed a little and never ruler-straight, with a dark edge on the side away
            # from the light and a thin glint on the other. Its colors are mixed from the skin it crosses, so it
            # works on a drow or a green hag as well as a farmhand. The swell under it comes from warp_image.
            import math as _m
            import random as _r
            a_s = min(1.0, scars)
            rs = _r.Random(w * 104729 + h)
            far = min(0.40, max(0.05, reach))
            lw_s = max(1.0, w / 200 * (0.6 + far * 2.4) * max(0.4, line))     # a longer scar is heavier too
            lit, rim_s = max(0.0, min(1.0, ridge)), max(0.0, min(1.0, edge))
            pucker = Image.effect_noise(im.size, 60).filter(ImageFilter.GaussianBlur(max(1, w // 300)))
            pucker = ImageChops.multiply(pucker.point(lambda x: max(0, min(255, 90 + x))),   # healed skin is
                                         Image.effect_noise((max(4, w // 40), max(4, h // 40)), 90)   # never even,
                                         .resize((w, h), Image.BICUBIC).point(lambda x: min(255, 70 + x)))  # nor whole
            for fx, fy in scar_points[:8]:
                cx, cy = fx * w, fy * h
                ang = _m.radians(angle + rs.uniform(-8, 8) if angle is not None else   # a set angle, give or take
                                 (rs.uniform(-80, -35) if rs.random() < 0.7 else rs.uniform(35, 80)))
                half = w * far * rs.uniform(0.30, 0.50)          # Reach is how long it runs
                pts, (nx, ny) = _scar_path(cx, cy, ang, half, rs, 0.10, 0.03)
                skin = _skin_near(out, cx, cy, half * 0.5)        # the skin it crosses, not the brow it may cross
                heal = _mix(_shade(skin, 1.20, 0.80, 14), (222, 170, 158), 0.12)   # paler, a touch of pink
                groove, shine = _shade(skin, 0.40, 0.85), _shade(skin, 1.5, 0.30, 50)
                core, dark, glint = (Image.new("L", im.size, 0) for _ in range(3))
                _taper(ImageDraw.Draw(core), pts, lw_s * 1.3)
                _taper(ImageDraw.Draw(dark), pts, lw_s * 1.3 + 1, shift=(nx * lw_s * 0.5, ny * lw_s * 0.5))
                _taper(ImageDraw.Draw(glint), pts, max(1.0, lw_s * 0.45), shift=(-nx * lw_s * 0.4, -ny * lw_s * 0.4),
                       ends=0.1)
                dark = ImageChops.subtract(dark, core).filter(ImageFilter.GaussianBlur(max(0.8, lw_s * 0.35)))
                core = ImageChops.multiply(core.filter(ImageFilter.GaussianBlur(max(0.7, lw_s * 0.30))), pucker)
                glint = glint.filter(ImageFilter.GaussianBlur(max(0.6, lw_s * 0.25)))
                out = Image.composite(Image.new("RGB", im.size, groove), out,
                                      dark.point(lambda x: int(x * a_s * (0.25 + 0.45 * rim_s))))
                out = Image.composite(Image.new("RGB", im.size, heal), out, core.point(lambda x: int(x * a_s * 0.65)))
                if lit > 0:
                    out = Image.composite(Image.new("RGB", im.size, shine), out,
                                          glint.point(lambda x: int(x * a_s * lit * 0.6)))
        if wounds > 0 and scar_points:
            # A fresh wound: a ragged dark cut with raw red lips, the skin round it swollen and reddened along
            # the cut (not a round bruise stamped on), and a little of it run down the face.
            import math as _mw
            import random as _rw
            a_w = min(1.0, wounds)
            rw = _rw.Random(w * 15485863 + h)
            far = min(0.40, max(0.05, reach))                    # Reach is how long the cut runs
            lw_w = max(1.0, w / 170 * (0.6 + far * 2.4) * max(0.4, line))
            deep, raw, bruised = BLOOD_TINTS.get(blood) or BLOOD_TINTS["Blood"]
            for fx, fy in scar_points[:8]:
                cx, cy = fx * w, fy * h
                ang = _mw.radians(angle + rw.uniform(-8, 8) if angle is not None else
                                  (rw.uniform(-75, -30) if rw.random() < 0.6 else rw.uniform(30, 75)))
                half = w * far * rw.uniform(0.24, 0.44)
                pts, (nx, ny) = _scar_path(cx, cy, ang, half, rw, 0.08, 0.025)
                skin = _skin_near(out, cx, cy, half * 0.5)
                swell, cut, lips, drip = (Image.new("L", im.size, 0) for _ in range(4))
                _taper(ImageDraw.Draw(swell), pts, lw_w * 7, ends=0.45)
                _taper(ImageDraw.Draw(lips), pts, lw_w * 2.6)
                _taper(ImageDraw.Draw(cut), pts, lw_w * 1.4, ends=0.15)
                dd_ = ImageDraw.Draw(drip)
                for _d in range(1 + int(3 * a_w)):               # a little of it has run, downwards
                    px_, py_, _t = pts[rw.randrange(len(pts) // 5, len(pts) - len(pts) // 5)]
                    ln_ = w * far * rw.uniform(0.10, 0.30)
                    wd_ = max(1.0, lw_w * rw.uniform(0.5, 0.9))
                    run_ = [(px_ + rw.uniform(-1, 1) * i_ * 0.4, py_ + ln_ * i_ / 8, -1 + i_ / 8) for i_ in range(9)]
                    _taper(dd_, run_, wd_, ends=0.5)
                    dd_.ellipse([run_[-1][0] - wd_, run_[-1][1] - wd_ * 0.6, run_[-1][0] + wd_,
                                 run_[-1][1] + wd_ * 1.2], fill=255)  # the bead at the end
                swell = swell.filter(ImageFilter.GaussianBlur(max(2, lw_w * 2.5)))
                lips = ImageChops.subtract(lips, cut).filter(ImageFilter.GaussianBlur(max(0.8, lw_w * 0.4)))
                cut = cut.filter(ImageFilter.GaussianBlur(max(0.6, lw_w * 0.25)))
                drip = drip.filter(ImageFilter.GaussianBlur(max(0.6, lw_w * 0.2)))
                inflamed = _mix(_shade(skin, 0.92, 1.1), raw, 0.38)
                out = Image.composite(Image.new("RGB", im.size, _mix(inflamed, bruised, 0.25)), out,
                                      swell.point(lambda x: int(x * a_w * 0.65)))
                out = Image.composite(Image.new("RGB", im.size, _mix(raw, deep, 0.25)), out,
                                      lips.point(lambda x: int(x * a_w * 0.85)))
                out = Image.composite(Image.new("RGB", im.size, deep), out, cut.point(lambda x: int(x * a_w)))
                out = Image.composite(Image.new("RGB", im.size, _mix(deep, raw, 0.35)), out,
                                      drip.point(lambda x: int(x * a_w * 0.9)))
                if ridge > 0:                                    # wet, so it catches the light
                    glint = Image.new("L", im.size, 0)
                    _taper(ImageDraw.Draw(glint), pts, max(1.0, lw_w * 0.35),
                           shift=(-nx * lw_w * 0.6, -ny * lw_w * 0.6), ends=0.05)
                    glint = glint.filter(ImageFilter.GaussianBlur(0.8))
                    out = Image.composite(Image.new("RGB", im.size, _shade(raw, 1.5, 0.35, 60)), out,
                                          glint.point(lambda x: int(x * a_w * min(1.0, ridge) * 0.5)))
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
                if (ox * ox + (oy / 0.84) ** 2) ** 0.5 > rr * w:   # inside the ring that was marked, never past it
                    continue
                cx, cy = fx * w + ox, fy * h + oy
                r_ = max(0.6, w * rp.uniform(0.0030, 0.0080))
                dd.ellipse([cx - r_, cy - r_ * 0.9, cx + r_, cy + r_ * 0.9], fill=rp.randrange(150, 256))
            dots = ImageChops.multiply(dots, skin_here)          # on skin, not on hair, cloth or the background
            dots = dots.filter(ImageFilter.GaussianBlur(max(0.6, w / 900)))
            # Darker than whatever they fall on, rather than one flat color, so the paint underneath keeps its
            # texture - but with a floor taken from the face's own level, or darkening the skin would take the
            # speckles down with it and a weathered face would end up smooth again.
            lvl = out.convert("L")
            inked = [v for v, m in zip(lvl.getdata(), skin_here.getdata()) if m > 40]   # getdata: every Pillow
            ref = sorted(inked)[len(inked) // 2] if inked else 128
            deep = max(-1.0, min(1.0, speckle_depth))           # how they sit against the skin: - lighter than
            mag = abs(deep)                                      # it, + darker. How many is only how many.
            step = max(1, int(ref * (0.08 + 0.45 * mag)), int(34 * mag))   # a floor too: on skin darkened almost
            #                                            to nothing, a share of very little is not a marking
            if deep >= 0:
                mul = 1.0 - 0.62 * mag
                shade = Image.merge("RGB", [c.point(lambda x: max(0, min(x - step, int(x * mul))))
                                            for c in out.split()])
            else:
                mul = 1.0 + 0.70 * mag
                shade = Image.merge("RGB", [c.point(lambda x: min(255, max(x + step, int(x * mul))))
                                            for c in out.split()])
            lay = max(0.0, min(1.0, speckle_amount))             # Strength does this and nothing else
            out = Image.composite(shade, out, dots.point(lambda x: int(x * lay)))
        try:
            _paint_ok = paint != "None" and paint_amount > 0 and bool(paint_eyes) and len(paint_eyes) >= 2   # type: ignore[arg-type]
        except TypeError:
            _paint_ok = False
        if _paint_ok:
            # Face paint, laid on like paint: following the face it is on (placed from the two eyes - marked, or
            # where these portraits put them), dry-brushed at the edges, and mixed so the skin's texture still
            # shows through. It stays off the whites of the eyes and out of the hair.
            import random as _rq
            a_q = min(1.0, paint_amount)
            rq = _rq.Random(w * 7907 + h * 31)
            ey = sorted(paint_eyes[:2])                          # type: ignore[index]  where it goes: from the eyes
            (lx, ly), (rx, ry) = (ey[0][0] * w, ey[0][1] * h), (ey[1][0] * w, ey[1][1] * h)
            d = max(8.0, ((rx - lx) ** 2 + (ry - ly) ** 2) ** 0.5)
            mx, my = (lx + rx) / 2, (ly + ry) / 2
            k = max(0.5, min(2.0, paint_size))
            m = Image.new("L", im.size, 0)
            dm = ImageDraw.Draw(m)

            def bar(x0: float, y0: float, x1: float, y1: float, wd: float) -> None:
                _taper(dm, [(x0 + (x1 - x0) * i / 10, y0 + (y1 - y0) * i / 10, -1 + i / 5) for i in range(11)],
                       wd * k, ends=0.55)
            if paint == "Cheek stripes":
                for ex, sd in ((lx, -1), (rx, 1)):
                    for i in range(3):
                        yy = my + d * (0.42 + 0.20 * i * k)
                        bar(ex + sd * d * 0.30, yy, ex - sd * d * 0.12, yy + d * 0.05, d * 0.075)
            elif paint == "Eye band":
                pts = [(lx - d * 0.62, my - d * 0.10), (mx, my - d * 0.26 * k), (rx + d * 0.62, my - d * 0.10),
                       (rx + d * 0.55, my + d * 0.22 * k), (mx, my + d * 0.20 * k), (lx - d * 0.55, my + d * 0.22 * k)]
                dm.polygon(pts, fill=255)
            elif paint == "Slash":
                bar(lx - d * 0.45, my - d * 0.85, rx + d * 0.05, my + d * 1.25, d * 0.16)
            elif paint == "Center line":
                bar(mx, my - d * 1.05, mx, my + d * 1.95, d * 0.13)
            elif paint == "Dots":
                for ex, sd in ((lx, -1), (rx, 1)):
                    for i in range(5):
                        cx_, cy_ = ex + sd * d * (-0.22 + 0.13 * i), my + d * (0.38 + 0.05 * i * i * 0.3)
                        r_ = d * 0.045 * k
                        dm.ellipse([cx_ - r_, cy_ - r_, cx_ + r_, cy_ + r_], fill=255)
            elif paint == "Chin lines":
                for i in (-1, 0, 1):
                    bar(mx + i * d * 0.16, my + d * 1.55, mx + i * d * 0.18, my + d * 2.10 * min(1.15, k), d * 0.07)
            brush = Image.effect_noise(im.size, 70).filter(ImageFilter.GaussianBlur(max(1, w // 350)))
            ragged = ImageChops.multiply(m.filter(ImageFilter.GaussianBlur(max(1.5, d * 0.035))),
                                         brush.point(lambda x: max(0, min(255, (x - 40) * 2))))
            ragged = ragged.point(lambda x: 255 if x > 110 else int(x * 2.2))  # solid inside, broken at the edge
            ragged = ImageChops.multiply(ragged.filter(ImageFilter.GaussianBlur(0.8)),
                                         vc.point(lambda x: 255 if 16 <= x <= 238 else 0)   # not eye-whites or
                                         .filter(ImageFilter.GaussianBlur(1)))               # dark hair and lashes
            tint = PAINT_TINTS.get(paint_color) or PAINT_TINTS["Woad blue"]
            flat = Image.new("RGB", im.size, tint)
            if sum(tint) > 500:                                  # white and ash: a chalky layer over the skin
                painted = Image.blend(out, flat, 0.58)
            else:                                                # dark paint: stained into it, texture kept
                painted = Image.blend(ImageChops.multiply(out, flat.point(lambda x: min(255, x + 70))), flat, 0.45)
            out = Image.composite(painted, out, ragged.point(lambda x: int(x * a_q * 0.92)))
        if soot > 0 or mud > 0 or spatter > 0 or sweat > 0:
            # Dirt of different kinds, each on the skin only: soot smeared as if wiped with a hand, mud in
            # cracked patches, dried blood flecked over it, and the shine of sweat.
            import random as _rd
            rd = _rd.Random(w * 4111 + h * 17)
            on_skin = face.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(max(1, w // 90)))
            ds = max(0.4, min(2.5, dirt_size))

            def blots(sx: int, sy: int, sharp: float, lo: int, soft: int = 160) -> Image.Image:
                n1 = Image.effect_noise((max(4, int(w / sx / ds)), max(4, int(h / sy / ds))), 90).resize(
                    (w, h), Image.BICUBIC)
                n2 = Image.effect_noise((max(6, int(w / 5 / ds)), max(6, int(h / 5 / ds))), 70).resize(
                    (w, h), Image.BICUBIC)
                b_ = ImageChops.multiply(n1, n2).filter(ImageFilter.GaussianBlur(max(1, w // soft)))
                return ImageChops.multiply(b_.point(lambda x: max(0, min(255, int((x - lo) * sharp)))), on_skin)
            if soot > 0:                                         # wiped: smears longer than they are tall
                a_ = min(1.0, soot)
                sm = blots(6, 22, 3.4 * a_, 18, 55)
                dark_ = Image.merge("RGB", [c.point(lambda x: int(x * 0.30)) for c in out.split()])
                out = Image.composite(Image.blend(dark_, Image.new("RGB", im.size, (38, 36, 36)), 0.4), out,
                                      sm.point(lambda x: int(x * 0.85)))
            if mud > 0:                                          # caked: harder edges, earth brown
                a_ = min(1.0, mud)
                sm = blots(10, 12, 3.6 * a_, 52)
                crack = Image.effect_noise(im.size, 60).point(lambda x: 0 if x > 200 else 255)
                sm = ImageChops.multiply(sm, crack)
                mudc = Image.blend(Image.merge("RGB", [c.point(lambda x: int(x * 0.45)) for c in out.split()]),
                                   Image.new("RGB", im.size, (96, 70, 44)), 0.55)
                out = Image.composite(mudc, out, sm.point(lambda x: int(x * 0.9)))
            if spatter > 0:                                      # dried blood: flecks round a few centres
                a_ = min(1.0, spatter)
                fl = Image.new("L", im.size, 0)
                df = ImageDraw.Draw(fl)
                bb_ = on_skin.point(lambda x: 255 if x > 128 else 0).getbbox() or (0, 0, w, h)
                for _c in range(2 + int(2 * a_)):
                    cx_ = rd.uniform(bb_[0] + (bb_[2] - bb_[0]) * 0.25, bb_[0] + (bb_[2] - bb_[0]) * 0.75)
                    cy_ = rd.uniform(h * 0.30, h * 0.58)
                    spread = w * 0.07 * ds
                    for _k in range(int(25 + 70 * a_)):
                        ox, oy = rd.gauss(0, spread), rd.gauss(0, spread * 0.8)
                        r_ = max(0.6, w * rd.uniform(0.0015, 0.006) * (1.6 if rd.random() < 0.1 else 1.0))
                        if rd.random() < 0.15:                   # a drop that flew: drawn out one way
                            ln_ = r_ * rd.uniform(2, 5)
                            df.line([cx_ + ox, cy_ + oy, cx_ + ox + ox / max(1, abs(ox)) * ln_,
                                     cy_ + oy + oy / max(1, abs(oy)) * ln_ * 0.6], fill=230, width=max(1, int(r_)))
                        df.ellipse([cx_ + ox - r_, cy_ + oy - r_, cx_ + ox + r_, cy_ + oy + r_], fill=255)
                fl = ImageChops.multiply(fl.filter(ImageFilter.GaussianBlur(0.6)), on_skin)
                dried = Image.blend(Image.merge("RGB", [c.point(lambda x: int(x * 0.5)) for c in out.split()]),
                                    Image.new("RGB", im.size, (92, 22, 16)), 0.7)
                out = Image.composite(dried, out, fl.point(lambda x: int(x * min(1.0, 0.45 + 0.55 * a_))))
            if sweat > 0:                                        # beads and a sheen where the light falls
                a_ = min(1.0, sweat)
                bead = Image.new("L", im.size, 0)
                db_ = ImageDraw.Draw(bead)
                for _k in range(int(30 + 110 * a_)):
                    x_, y_ = rd.uniform(0, w), rd.uniform(h * 0.15, h * 0.62)
                    r_ = w * rd.uniform(0.0008, 0.0022)
                    db_.ellipse([x_ - r_, y_ - r_, x_ + r_, y_ + r_], fill=255)
                lightish = out.convert("L").point(lambda x: 255 if x > 95 else int(x * 2.4))  # sweat shines in light
                bead = ImageChops.multiply(ImageChops.multiply(bead.filter(ImageFilter.GaussianBlur(0.7)), on_skin),
                                           lightish)
                sheen = ImageChops.multiply(blots(8, 8, 2.0, 40), lightish)
                out = ImageChops.screen(out, Image.composite(Image.new("RGB", im.size, (120, 110, 100)),
                                                             Image.new("RGB", im.size, (0, 0, 0)),
                                                             sheen.point(lambda x: int(x * a_ * 0.45))))
                out = Image.composite(Image.new("RGB", im.size, (250, 246, 236)), out,
                                      bead.point(lambda x: int(x * a_ * 0.55)))
        if grime > 0:
            # The engine washes everybody: a peasant comes out as clean as a prince, and no wording stops it.
            # Dirt is laid on the skin only - not the hair, the clothes or the background - in blotches where
            # it gathers, mixed from the skin's own color made darker and browner, with fine grit over it.
            from PIL import ImageStat
            a_g = min(1.0, grime)
            on_skin = face.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(max(1, w // 90)))
            avg = tuple(int(c) for c in ImageStat.Stat(out, face).mean[:3]) if face.getbbox() else (150, 120, 100)
            dirt = _mix(_shade(avg, 0.42, 0.75), (70, 54, 38), 0.5)            # type: ignore[arg-type]
            big = Image.effect_noise((max(8, w // 12), max(8, h // 12)), 90).resize((w, h), Image.BICUBIC)
            mid = Image.effect_noise((max(8, w // 4), max(8, h // 4)), 70).resize((w, h), Image.BICUBIC)
            blot = ImageChops.multiply(big, mid).filter(ImageFilter.GaussianBlur(max(1, w // 90)))
            smear = ImageChops.multiply(blot.point(lambda x: max(0, min(255, int((x - 60) * 2.6 * a_g)))), on_skin)
            soiled = Image.blend(Image.merge("RGB", [c.point(lambda x: int(x * 0.52)) for c in out.split()]),
                                 Image.new("RGB", im.size, dirt), 0.35)   # darker than whatever it lies on, and
            out = Image.composite(soiled, out, smear.point(lambda x: int(x * 0.75)))   # browner

            grit = Image.effect_noise(im.size, 40).point(lambda x: 255 if x > 232 - int(10 * a_g) else 0)
            grit = ImageChops.multiply(grit.filter(ImageFilter.GaussianBlur(0.6)), on_skin)
            out = Image.composite(Image.new("RGB", im.size, _shade(dirt, 0.7, 1.0)), out,
                                  grit.point(lambda x: int(x * a_g * 0.6)))
            grain = Image.effect_noise(im.size, 13 * a_g)
            rough_ = Image.blend(out, ImageChops.overlay(out, Image.merge("RGB", (grain, grain, grain))), 0.35 * a_g)
            out = Image.composite(rough_, out, on_skin)
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
