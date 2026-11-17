# Portrait Scout

Finds the speaking creatures of an Infinity Engine mod - or of a whole game install (BG1EE, SoD, BG2EE, EET, IWDEE, and classic BG1, BG2, IWD) - and shows who talks, how much, what portrait each one has, and where in the game they turn up. Then it helps you fill the gaps: propose portraits, make new ones with an image generator, and export the lot as a WeiDU mod.

It only **reads** your game folder. Nothing there is installed, changed or deleted.

| Creature | Lines | Portrait | Race | Gender | Animation | CRE file(s) | Area(s) |
|---|---|---|---|---|---|---|---|
| Skie | 21 | SKIEL | Human | Female | Thief Female Human | SKIE | AR0700 Docks District |
| Urnn Indigospit | 11 | L#SSMAG | Human | Male | Mage Male Human | L#SSMAG | AR1203 Firkraag's Dungeon |

![Portrait Scout gallery](screenshot.png)

## Requirements

- Python 3.8+ with tkinter (included in the normal Windows/macOS installers; Linux: `python3-tk`).
- A game folder (one with `chitin.key`), and the mod's own folder. Not IWD2 or Planescape: Torment.
- Optional: [ffmpeg](https://ffmpeg.org) on PATH to play compressed sounds (WAVC/ACM, Ogg); plain WAV plays without it.
- Optional: `pip install pillow` for JPG previews and slightly sharper generated portraits.

## Use

1. Copy `portrait_scout.py` into the game folder and run `python portrait_scout.py` there. Both folders are remembered.
2. Pick the **Mod folder**. Leave it empty, or pick the game folder, to list the whole install.
3. Press **Analyze** (a minute or two on a large install).

| Tab | What it holds |
|---|---|
| **Table** | Every speaker, sortable, with lines, portrait, race, gender, animation, creature files and areas. |
| **Gallery** | The same creatures as cards with their portraits, 200 to a page. |
| **Generate** | Make a portrait for the selected creature, or any portrait at all. |
| **Proposals** | Everything you have chosen a picture for, and the exports. |

**Search** (or **Ctrl+F**) filters Table and Gallery as you type; every word must match. The gallery's **Sort** box orders by lines or by name, either way round (clicking a table heading does the same). **Party NPCs**, unticked, hides the joinable ones - the game's and the mods' alike. Both are remembered.

Creatures put in one row only because they share a name and look alike - a mod's 27 "Kobold Commoner" creatures, say - can be **listed one by one**, so each takes a portrait of its own: the button is in the creature's lines window, and in the right-click menu of its portrait. It is remembered per creature, and creatures that share a creature file or a portrait are never split. Needs the installed-game reading. Click a creature or CRE name to copy it for NearInfinity, and right-click a portrait to save it. **Double-click** a creature for its lines: **Copy** one or all, **▶ Play** the voiced ones, search them, and read "Where this comes from" for speakers that needed working out.

A `<ModName>-creatures.txt` report is saved next to the script; **Save as text file...** and **Save as CSV...** save elsewhere.

Dialogs of one NPC (a joined-party dialog, several creature files) are shown as one row when they share a creature file or portrait, or do not differ in race, gender and animation. **Merge same names** forces this by name alone - leave it off when different creatures share a name, though identical ones (several plain "Peasant" creatures) still merge, and their Area(s) column is the giveaway.

## Reading the mod's own files

Tick **Read the mod's own files** (marked *install not needed*) to work from the mod's `.d`, `.tra`, `.tp2`, `.cre`, `.baf` and `.are` files instead of matching its text against the installed game.

- The mod does not have to be installed, and every component is counted.
- Every line comes with its speaker, so short stock lines ("Yes.") are not credited to the wrong creature.
- The game folder is still needed: it names the NPCs the mod only adds lines to (Imoen, Jaheira...), through party tables, scripts, other mods' installers and, on EET, its own name tables.
- Dialogs of NPCs from mods that are not installed stay `Unknown (DIALOGNAME)`.
- **Show mod's own portraits only** switches the gallery to the pictures the mod itself ships, instead of what the game shows now.

## Generating portraits (optional)

The **Generate** tab works on the selected creature, or on nobody in particular - pick Race, Gender and Class and it writes the description for you.

> **No image generator and no models come with Portrait Scout.** They are many gigabytes and separately licensed. You supply one: a copy of [Stable Diffusion WebUI Forge](https://github.com/lllyasviel/stable-diffusion-webui-forge) in an `engine` folder, or any generator with an Automatic1111 / Forge API, including one you already run. **The Generate tab shows the steps with the download links as buttons**, and [generator/README.md](generator/README.md) has the long version. About 16 GB of disk and an 8 GB graphics card are comfortable; 6 GB works, slower.

- **Description** comes from the creature's gender, race and animation, plus a face drawn from the creature's own file - shape, nose, hair and sometimes a scar or a missing tooth. The same creature always gets the same face, and creatures sharing a portrait share it, so a decloned crowd comes out as a crowd of different people. Edit the text freely; what you type wins.
- **Age** and **Look** shade that face - *Old*, *Weathered*, *Blotchy and freckled* and so on. Flux paints clean young skin unless told otherwise, so they earn their place.
- **Background** is used by every style. `{random}` in it picks one of the muted earth schemes in `background_palettes` per picture, so a few thousand portraits do not all sit against the same wash.
- **Candidates**, **Style** and **LoRA** sit on the same row. LoRA off takes the `<lora:...>` part out, which matters for creatures with features of their own - horns, green skin - that a LoRA tends to paint away.
- **Start** and **Stop** run the generator. **Engine** holds the rest: keep it open after Portrait Scout closes, give it a window of its own, show its log, open its own page, edit `settings.json`.
- **Prompt** and **Generator settings** open the words sent to the generator and the model, picture size, steps, sampler and CFG. **Copy prompt** puts both prompts on the clipboard. **Save** keeps changes, **Revert to baseline** puts a built-in style back.
- Click a candidate for full size, right-click to save a copy, **Discard** to delete it. **Full size** off shows every candidate the same size for comparing; on shows each at its own.
- **Use this** crops a candidate to a game portrait - **speaker 169x266** or **large 210x330** from the size box - and proposes it. **Head zoom** above 1 crops closer, with a yellow box showing what is kept, and the arrows move that box on every candidate at once. Making a new game portrait replaces the one before it.

## Proposing portraits

**Propose portrait...** on a gallery card or in a lines window picks an image for a creature; it shows with a green border. A creature's lines window names the others using its portrait, and **Use for others...** hands the proposed picture to any of them - a quick way to give one new portrait to a whole decloned bunch. Proposals are kept for each thing you analyze and come back when you analyze it again - the **Proposals** tab lists them, with **Saved sets...** for the ones kept from before.

Any picture will do - **Create mod...** cuts it to the portrait's shape, shrinks it to the chosen size and saves it as a 24-bit BMP named after the creature file. BMP and PNG are handled on their own; `pip install pillow` adds JPG and the rest, and sharper shrinking.

**Export proposed changes...** writes a to-do list, one line per creature file:

```
B!ASTER.CRE  Aster -> BB12020.bmp   [now: none]   [areas: AR1000 Athkatla (placed)]
```

### Create mod

**Create mod...** turns the proposals into a ready-to-install WeiDU mod. It asks for a name and writes `mod\<name>\` next to `portrait_scout.py`, with a copy of WeiDU beside it (taken from your game folder):

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

- As in the HPS portraits mod, each picture is named after its creature file and written into both portrait slots. Whatever you proposed is converted on the way in, so a square photo or a PNG comes out as a correctly shaped game portrait.
- Each set - one per analyzed mod - is its own choice in the installer, offered twice: give the portraits to every creature listed, or only to those with none. Creature files not in the game are skipped.
- Edit `portraits/<set>/portraits.txt` and create the mod again to change what is installed, or edit the `.tp2` by hand.
- Creating it again with the same name updates that mod; another name starts a new one.

## Columns

![Portrait Scout table view](screenshot2.png)

- **Lines**: NPC lines the creature speaks (player replies excluded).
- **Portrait**: large portrait name, or `(none)`, `(file missing)`, `(creature not found)`.
- **Race / Gender / Animation**: from the creature file and the game's `race.ids` / `animate.ids`.
- **Area(s)**: the maps the creature appears in, by code and world-map name - placed by the area file, made by a spawn point, or created by the area's script. Creatures that only appear through a dialog or cutscene have none.
- **CRE file(s)**: creature files sharing the dialog. **Bold** (`[brackets]` in the table) means the mod ships or names that file.
- **Proposed portrait**: your proposal (table only).

The text report also lists portraits shared by several creatures, and how many have none.

## Command line

```
python portrait_scout.py --cli "C:\Games\BG2EE" "C:\Games\BG2EE\MyMod" --out creatures.txt --csv creatures.csv
python portrait_scout.py --source "C:\Games\BG2EE\MyMod" --game "C:\Games\BG2EE" --out speakers.txt
```

`--cli` analyzes as the window does (`--merge-names` merges same-named creatures); `--source` reads the mod's own files.

## Notes

- It reads `chitin.key`, the archives, override and `dialog.tlk` directly. Only plain (uncompressed) archives are read, as in the Enhanced Editions.
- Without **Read the mod's own files**, matching is by text: accurate, not perfect. Unlinked speakers show as `Unknown (DIALOGNAME)`, and the lines window and the CSV `notes` column explain what is known.
- Folders, proposals and your choices live in `portrait_scout.settings.json` next to the script; delete it to start over.
