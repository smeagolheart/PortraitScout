# Portrait Scout

Finds the speaking creatures of an Infinity Engine mod - or a whole install (BG1EE, SoD, BG2EE, EET, IWDEE, and classic BG1, BG2, IWD) - and shows who talks, how much, what portrait each one has, and where they turn up. Then it helps you fill the gaps: propose portraits, generate new ones, and export the lot as a WeiDU mod.

It only **reads** your game folder. Nothing there is installed, changed or deleted.

**To generate portraits** you need an image engine (not included): see **[Setting up the portrait generator](generator/README.md)** for the steps and download links, and get the face bank from the [release](https://github.com/smeagolheart/PortraitScout/releases). The same steps open in Portrait Scout the first time it starts without an engine, and any time from **Generator setup** at the top right.

| Creature | Lines | Portrait | Race | Gender | Animation | CRE file(s) | Area(s) |
|---------------------------------------------------------------------------------|
| Skie | 21 | SKIEL | Human | Female | Thief Female Human | SKIE | AR0700 Docks District |
| Urnn Indigospit | 11 | L#SSMAG | Human | Male | Mage Male Human | L#SSMAG | AR1203 Firkraag's Dungeon |

![Portrait Scout gallery](screenshot.png)

## Requirements

- Python 3.8+ with tkinter (included in the normal Windows/macOS installers; Linux: `python3-tk`).
- A game folder (one with `chitin.key`), and the mod's own folder. Not IWD2 or Planescape: Torment.
- Optional: [ffmpeg](https://ffmpeg.org) on PATH to play compressed sounds (WAVC/ACM, Ogg); plain WAV plays without it.
- Optional: `pip install pillow` for JPG previews and slightly sharper generated portraits.

## Use

1. Put `portrait_scout.py` in a folder of its own and run `python portrait_scout.py` there. It writes its reports, proposals, generated pictures and any mod it builds beside itself, so keep it out of the game folder.
2. Set the **Game folder** (the one with `chitin.key`). It is remembered, along with the mod folder.
3. Pick the **Mod folder**. Leave it empty, or pick the game folder, to list the whole install.
4. Press **Analyze** (can take a minute or two on a large install).

| Tab                 | What it holds |
|-------------------------------------|
| **Gallery**         | Creatures as cards with their portraits, 200 to a page. Opens here. |
| **Table**           | Every speaker, sortable, with lines, portrait, race, gender, animation, creature files and areas. |
| **Propose**         | Choose a picture for a creature, or make one, and crop it to a game portrait. |
| **Proposals**       | Everything you have chosen a picture for, and the exports. |

**Search** (or **Ctrl+F**) filters Table and Gallery; every word must match. **Sort** orders by lines or name, either way round - so does clicking a table heading. **Party NPCs** off hides the joinable ones. Both are remembered.

**Double-click** a creature for its lines: play the voiced ones, search them, and see "Where this comes from" when a speaker had to be worked out. Click a creature or CRE name to copy it for NearInfinity; right-click a portrait to save it.

Creatures merged only because they share a name and look alike - a mod's 27 "Commoner" - can be **listed one by one** (decloned), so each gets its own portrait from a button on the gallery card, in the lines window and in the portrait's right-click menu; remembered per creature. Creatures sharing a single creature file are never split. It works both ways round now: reading the mod's own files, a bunch the mod can only name (the game's own commoners) splits one creature per dialog.

Two creatures **of the same name** become one row when they share a creature file or the same portrait file, or do not differ in race, gender and animation - so two different "Guard" creatures stay apart. Creatures with different names are never merged, however alike they look.
(The command line's `--merge-names` still forces it by name alone.)

A `<ModName>-creatures.txt` report is written next to the script each time, and **Save list...** on the Proposals tab saves it elsewhere. As text it is that same report: the table, plus a list of portraits used by several creatures and a count of how many have none. As `.csv` (pick the type in the save window) it writes more per creature instead - the small portrait on its own, the pixel size of each, the dialog files, and a `notes` column saying how an uncertain speaker was worked out.

## Reading the mod's own files

When the mod is not installed in the game folder you picked, **Analyze** works from the mod's own `.d`, `.tra`, `.tp2`, `.cre`, `.baf` and `.are` files by itself, instead of matching its text against the installed game. (From the command line: `--source`.)

- The mod need not be installed, all components get counted.
- Every line comes with its speaker (Stock lines ("Yes.") are not credited to the wrong creature).
- The game folder is still needed: it names the NPCs the mod only adds lines to (Imoen, Jaheira...).
- Crossmod NPC dialogs that are not installed are undetectable; they show as `Unknown (DIALOGNAME)`.
- **Show mod's own portraits only** switches the gallery to the pictures the selected mod itself ships.

## Generating portraits (optional) (**No image generator and no models come with Portrait Scout**)

The **Propose** tab works on the selected creature, or can be used to freely generate images - pick Race, Gender and Role and it writes the description for you.

Image Generators are large files and separately licensed. 

Supply your own, suggested working steps below: 

Two engines are set up for, and anything else with an Automatic1111 / Forge API works as well:

- **[Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo) with FLUX.2 Klein 9B**, in an `engine-neo` folder - what the newest styles are written against. Cloned with git, three model files (~18 GB), 4 steps at CFG 1. 16 GB of graphics memory at fp8.
- **[Stable Diffusion WebUI Forge](https://github.com/lllyasviel/stable-diffusion-webui-forge) with FLUX.1 dev**, in an `engine` folder - a 1.7 GB package and one 12 GB model file, no git. An 8 GB card is comfortable; 6 GB works, slower.

 **The Propose tab shows the steps of either with download links as buttons** (pick the engine at the top of the panel); [generator/README.md](generator/README.md) has the detailed instructions, the model links and the `settings.json` to go with each.

- **Description** comes from the creature's gender, race and animation, plus a face drawn from its own file - shape, nose, hair, sometimes a scar. The same creature always gets the same face, and creatures sharing a portrait share it, so a decloned crowd comes out as different people. Edit it freely; what you type wins.
- **Race**, **Gender** and **Role** start from the creature's own file and do nothing until you change one, which rewrites the description. With no creature selected they are the whole description.
- Each picker says one thing:
  - **Role** is who they are, one for each kind of person the games have a sprite for: Fighter, Cleric, Mage, Thief, Monk, Soldier, Noble, Peasant, Beggar, Sailor, Courtesan. It also picks what they wear.
  - **Age**.
  - **Look** is the skin. Flux paints clean young skin unless told otherwise, so *Weathered* and *Blotchy and freckled* might help.
  - **Scars** goes None, Few (the default), Scarred, Heavy, Wounded. **Grime** goes Clean, Smudged, Dirty, Filthy.
  - **Mood** is the expression. *Neutral* asks for a calm one. *Any* lets Vary faces pick one for each picture, mostly calm: neutral, a faint smile, thoughtful, the odd grin. Angry and Fierce are never picked for you; choose them as the Mood.
  - **Outfit** is what they wear. *By role* takes the role's own: padded armor for a fighter, a homespun tunic or kirtle for a peasant, a velvet doublet or gown for a noble. Pick one to put a fighter in mail or a peasant in rags. The creature's colors go into it.
  - Sprites with a look of their own keep it: the Fat Man is drawn very fat, bald and clean-shaven, and the Ogre, Half Ogre and Hobgoblin in their horned helmets.
  - The lists live in `role_terms`, `outfit_terms`, `look_terms`, `scar_terms`, `grime_terms`, `mood_terms` and `sprite_terms` in `generator/settings.json`.
- **Background** is what is behind the creature in the image. `{random}` picks one of the muted earth schemes in `background_palettes` for generating a generic Baldur's Gate style background.
- **LoRA** off drops the `<lora:...>` part - worth it for creatures with features of their own, like horns or green skin, that a LoRA might paint away.
- **Select image...** takes a picture you already have. With no generator set up, **Generate** is grayed and a notice offers **Set it up...**; choosing a file still works. **Hide generator settings** folds away the description, the pickers, the background and that notice, for when you are only working from files.
- **Engine** holds the generator's own settings such as: keep it running after Portrait Scout closes, give it a window, show its log, open its page, edit `settings.json`.
- **Prompt** and **Generator settings** hold the words sent and the model, size, steps, sampler and CFG, **Revert to baseline** restores a built-in style.
- **Full size**, off, shows every candidate at one size for comparing; right-click an image to save a copy.
- **Use this** crops a candidate to **169x266** or **210x330** and proposes it. **Head zoom** above 1 crops closer. A yellow box shows what would be kept; use arrows buttons to move that box (affects all candidates at once).

## Proposing portraits

**Propose portrait...** on a gallery card or in a lines window opens the **Propose** tab for that creature, where **Select image...** takes a picture from a file and **Generate** makes one. Either way it lands with the candidates, so the crop box applies, and what you keep shows on the card with a green border. A lines window names the others using that portrait, and **Use for others...** hands the picture to any of them - handy after decloning. Proposals are kept per analyzed mod and come back when you analyze it again; the **Proposals** tab lists them, with **Saved sets...** for earlier ones.

### Proposing in bulk

**Propose from folder...** matches a folder of pictures to creatures by name and proposes the lot in one press. Portrait projects name a picture after its creature file.  For example, it would match `ACHEN.bmp` to `ACHEN.CRE`.  If you use the mod creator function, **Create mod** writes `ACHEN.bmp` to `ACHEN.CRE`, so an exported set can be read straight back in. It also matches the *portrait* name, so `HALFG3.bmp` covers every creature wearing HALFG3. Subfolders are read; proposals manually selected not replaced with files in folders using this method. Finally, you see what would happen *7,589 pictures, matching 1,804 of the 3,357 creatures listed* before writing anything.

**Select**, in the gallery toolbar, puts a tick box on every card, with **All**, **Proposals** and **None**. Ticks survive sorting and paging. **Use portraits they have now** proposes, for each ticked creature, the portrait it is wearing in your game, copied out of `override`. This could be useful to make sure someone who installs the mod gets all the same portraits that you see in the gallery. A creature can look covered on your install only because another mod put a picture there; a mod built from that has a hole in it for everyone else. This finds them.

Pictures that come from baseline game installed files aren't included.  Hand-picked proposals aren't overwritten. You will get a report that shows how many were taken, how many were already the game's, and how many have no portrait at all. That last number is the one to act on: those are the gaps nothing has filled.

Any picture will do - **Create mod...** cuts each one to the portrait's shape, shrinks it to the selected size and saves it as a 24-bit BMP named after the creature file, so a square photo is cropped rather than squeezed. BMP and PNG work on their own; `pip install pillow` adds JPG and the rest.

**Save to-do list...** writes one, one line per creature file, without creating a WeiDU mod.

### Start from: the face bank

The model paints nearly the same face for the same words, whatever the seed. **Start from** fixes that: each
picture starts from a real face, which the words then turn into the character.

- **Face bank** (default): a new face from the `faces` folder for every picture, of the picture's own gender
  (`faces\human\male`, `faces\human\female`). `faces\human` is used for any race without a folder of its own
  (`faces\elf`...), except duergar and svirfneblin, which are left to their words; children only use
  `faces\child`. Faces are dealt like a shuffled deck, so none comes back until all have been used. Monsters
  with a face like a person's (orcs, half-ogres, goblins, xvarts...) are painted in two passes: the bank face
  first, then the creature in words. Beasts, kobolds and the undead are left to the words.

  **Getting the faces:** the face bank is not in the repository (about 550 MB). Download
  `PortraitScout-2.0-faces.zip` from the [release](https://github.com/smeagolheart/PortraitScout/releases) and
  unzip it into the Portrait Scout folder, so there is a `faces` folder beside `portrait_scout.py`. The photos
  are synthetic faces from [SFHQ](https://github.com/SelfishGene/SFHQ-dataset) by David Beniaguev (MIT License).
  Without them, Start from works as Nothing.
- **Its portrait**: starts from the creature's own portrait (the face bank if it has none).
- **Nothing**: plain, as before.

The number beside it is the strength (0.68): about 0.6-0.72 works; lower keeps more of the start face. Bank
pictures are painted in 6 steps (`face_bank` in generator/settings.json), which at 0.68 costs the same as 4 plain
ones. Any picture can be added to a bank folder (656x1024 is best; others are cut to fit).

### A portrait for every creature (the gallery's Expanded view)

The **Gallery** opens on its **Expanded view** (called the Auto view before), which makes pictures for every creature listed in one go. The filters above (search, chips, party NPCs) decide which creatures are listed. Untick **Expanded**, beside the Gallery button, for the usual cards. The choice is remembered.

**What it makes:**
- Each creature gets **2** candidates (set by **Each**), made exactly as the Propose tab would make them: the creature's own description, role, sprite look, colors and face, with Vary faces on.
- A description you've already edited in Propose is used as it is.
- Animals get the Animal style, and ghosts are faded.
- Generating takes about 2-3 seconds a picture on a fast engine. **Stop** stops after the picture in hand, and **Generate** again carries on where it left off.

**Choosing:**
- The candidates show two creatures to a row, and the whole page scrolls.
- **Click** a picture to pick it: it turns blue, **● Picked A**. Click it again to take the pick back. Right-click opens a picture full size.
- A creature that already had a portrait or a proposal also shows it, first, as **Keep**. Pick that to keep it and use none of the new ones. A kept creature isn't made again.
- **Apply picks** proposes everything picked in one go: each picture is cut to the portrait size and head zoom set in Propose (169x266 by default), just like **Use this**, and turns green, **✔ Proposed A**. Picks wait until you apply them, between sessions too.
- **Crop**, above Reroll, moves the yellow box on a creature's pictures, which shows what the portrait keeps: − and + zoom, the arrows move it, and **Mid** goes back to the crop Propose uses. A picture already proposed is cut again to match.
- **Reroll** throws away a creature's candidates and makes new ones. **Open in Propose** lets you work on it by hand. **Discard** deletes its pictures and takes it off the list.
- **Discard unchosen** deletes the leftover candidates of every creature you've picked for. **Hide picked** shows only the ones with nothing picked or proposed.

**Which creatures are left out:**
- Creatures that already have a proposal, unless **Redo proposed** is ticked. Picking a new picture then replaces the proposal.
- Party NPCs, unless **Party NPCs** is ticked.
- Creatures with a portrait of their own are *included* by default, so the whole mod comes out in one look. Tick **Skip if has one** to leave them alone.
- Creatures known only by a name are drawn from their race and gender.

### Touching a portrait up

**Touch up** on a candidate has a tab each for Skin, Lips, Spots, Shape and Picture, and works on the picture after the engine has painted it, because the engine paints what it recognizes whatever the description says - a commoner comes out as clean and handsome as a prince, and a broken nose comes out straight.

It works on a finished portrait too, not only a generated picture: the portrait a creature wears in the game joins the candidates in Propose, so a stock face can be scarred, speckled or recolored and proposed back. A picture that is already portrait-sized comes back the same size, with nothing cut out of it a second time, so **Propose** under it makes it the creature's portrait as it stands.

The window has its own **zoom**, so a 169x266 game portrait is not worked on at stamp size; the marks you place scale with it. Ctrl+wheel zooms, the wheel scrolls.

Skin, lips and a **spot color** for anything you click - an eye, a gem, a cloak trim - are recolored from what is already there, so a sampled hue is moved rather than painted over. **Creases**, **grime** and a **ghostly** fade apply to the whole face.

A click on the picture lands on the tab you are on. Click to put a **shape point** down, then either push the face out of shape (**Swell**, with **Spread** for how wide an area moves) or cut it: a healed **scar** or a fresh **wound**, with **Length**, **Width**, **Angle**, **Ridge** (how much light the edge catches) and **Edge** (the dark line the shipped portraits draw round every feature). A scar is mixed from the skin's own color where it falls, so it reads on a drow or a dragon as well as on a farmhand; a wound takes a color - blood, or ichor for the things that have none.

**Light it** uses the same marks: a **glow** or a **flame** in the shape of an eye rather than a disc, with **Spill** for how far the light falls on the brow and the cheekbone, and **Shadow** for the pits an undead thing has where its eyes were.

**Paint these in** settles the pass into the picture and clears the marks, so the next scar can have a width, color and angle of its own. **Undo** takes back a step, a pass included.

### Handing work to someone else

If a proposed picture is deleted - re-cropping replaces the crop it was made from - a red
**Missing picture(s)...** button appears on the Proposals tab. It points each one at another crop of the same
picture where there is one, and names any it cannot place.

**Export work...** on the Proposals tab writes one `.zip` holding the pictures you have chosen, which creature each one is for, the creatures you have set aside, which bunches you had decloned, and what each picture was asked for - plus a readable `how-these-were-made.txt`. **Import work...** takes one in: the pictures are copied into this copy's generated folder, the paths are rewritten and the creatures are matched to the ones here, which works even when the two of you read the mod differently. Anything you have already chosen is kept unless you say otherwise.

### Preview page

**Preview page...** on the Proposals tab saves a single web page of the proposed portraits, at game size, each with its name, race and gender. Open it in any browser to look the set over or send it to someone: there is a search box, and clicking a portrait shows it bigger. **Create mod...** also puts one in the mod, and **Export work...** in the work's .zip, as `preview.html`.

### Create mod

**Create mod...** turns your proposals into a ready-to-install WeiDU mod. It asks for a name and writes `mod\<name>\` next to `portrait_scout.py`, with a copy of WeiDU beside it (taken from your game folder):

```
mod/
  setup-my_portraits.exe        a copy of WeiDU
  my_portraits/
    setup-my_portraits.tp2      the installer
    readme.txt                  what it is and how to install it
    portraits.txt               set, creature file, picture, creature, areas
    my_portraits.ini            so Project Infinity lists it
    portraits/blackhearts/      b!aster.bmp ...  portraits.txt
```

- Each picture is named after its creature file and written into both portrait slots. 
- Each set - one per analyzed mod - is its own choice in the installer, offered twice: give the portraits to every creature listed, or only to those with none.  
- Creatures not present in your installed game will be skipped.

### Creatures a mod copies

A quest mod often builds a creature by copying one of the game's own - CotSC makes its Bassilus with
`COPY_EXISTING ~BASSIL.CRE~ ~override/QRBSLUS.CRE~`. The copy keeps whatever the original had, portrait
included, so the same face turns up on both. Install the mod before whatever gave that original its portrait
and the copy gets nothing at all, which is why a creature can lose its face when you reinstall.

Analyze reads the mod's installer for those copies. A creature that wears a face only because it was copied
is marked **borrowed** on its card, the Portrait column reads `(none) <- FARM`, and the creature's window
names the file it came from. A dozen creatures wearing the same borrowed face is the clearest sign of where
a portrait of its own is wanted.

**Create mod...** writes those creatures in, so the copy keeps looking like the original whatever order the
mods were installed in. **Everything I see** does the lot; untick it to decide separately whether copies of a
stock farmer or merchant are included (that picture is whatever each install puts there), and whether the
pictures are carried in the mod or only named.

- You can manually edit `portraits/<set>/portraits.txt` and create the mod again to change what installs. Same name updates that mod; another name starts a new one.

## Columns

![Portrait Scout table view](screenshot2.png)

- **Lines**: NPC lines spoken (player replies excluded).
- **Portrait**: large portrait name, or `(none)`, `(file missing)`, `(creature not found)`.
- **Race / Gender / Animation**: from the creature file and the game's `race.ids` / `animate.ids`.
- **Area(s)**: maps the creature appears in, by code and world-map name - placed by the area file, made by a spawn point, or created by its script. Creatures seen only in a dialog or cutscene have none.
- **CRE file(s)**: creature files sharing the dialog. **Bold** (`[brackets]` in the table) means the mod ships or names that file.
- **Proposed portrait**: your proposal (table only).


## Command line

```
python portrait_scout.py --cli "C:\Games\BG2EE" "C:\Games\BG2EE\MyMod" --out creatures.txt --csv creatures.csv
python portrait_scout.py --source "C:\Games\BG2EE\MyMod" --game "C:\Games\BG2EE" --out speakers.txt
```

`--cli` analyzes as the window does (`--merge-names` merges same-named creatures); `--source` reads the mod's own files.

## Tests

```
python test_portrait_scout.py
```

Covers the parts that run without a game folder, a window or an image engine: the .CRE struct offsets and the
two readers that use them, the WeiDU `.d` parser, text normalizing, the color words a creature's palette turns
into, the mod-export helpers, and cutting any picture down to a game portrait.

## Notes

- It reads `chitin.key`, the archives, override and `dialog.tlk` directly. Only uncompressed archives, as in the Enhanced Editions.
- Without **Read the mod's own files**, matching is by text: accurate, not perfect. Unlinked speakers show as `Unknown (DIALOGNAME)`; the lines window and the CSV `notes` column say what is known.
- There is a command line for scripting and debugging - `python portrait_scout.py --help` lists it.
- Folders, proposals and your choices live in `portrait_scout.settings.json` next to the script; delete it to start over.
