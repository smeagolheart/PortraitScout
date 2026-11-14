# Portrait Scout

A small standalone tool that lists the speaking creatures of an **installed** Infinity Engine mod (BG1EE, SoD, BG2EE, EET, IWDEE and the classic BG1, BG2, IWD), or of the whole install, with:

| Creature | Lines | Portrait | Race | Gender | Animation | CRE file(s) |
|---|---|---|---|---|---|---|
| Skie | 21 | SKIEL | Human | Female | Thief Female Human | SKIE |
| Urnn Indigospit | 11 | L#SSMAG | Human | Male | Mage Male Human | L#SSMAG |

It is meant for planning portraits and art for a mod's NPCs: who talks, how much, which portrait file each one already uses (so you can spot duplicates or missing ones), what kind of creature it is, and which creature file to open in NearInfinity. You can also propose a new portrait for each creature and export the proposals as a to-do list.

It only **reads** your game folder. It never changes anything in it.

## What you need

- Python 3.8 or newer (tkinter is included with the normal Windows and macOS installers; on some Linux systems install `python3-tk`).
- The game folder with the mod already installed through WeiDU (the folder that contains `chitin.key`). Not IWD2 or Planescape: Torment (different creature file format).
- The mod's own folder (the one with its `.tp2`, `.tra` and `.d` files), unless you want the whole install.
- Optional: `pip install pillow` lets the window preview JPG and other image types for proposed portraits. Without it, PNG, GIF and BMP preview fine.

No dialog report and no other tools are needed.

## Using the window

1. Copy the script into your game folder (the one with `chitin.key`), open a command prompt there and run `python portrait_scout.py`. The window starts with the current folder as the game folder.
2. **Game folder:** pre-filled when you run it from the game folder; otherwise browse to it. The last folder used is remembered.
3. **Mod folder:** browse to the mod's folder. **Leave it empty, or pick the game folder itself, to list every creature in the whole install.**
4. Press the green **Analyze mod** button. On a large install this can take a minute or two.
5. Switch between **Table** and **Gallery** with the two buttons next to Analyze:
   - **Table:** click a column header to sort, click again to reverse. Click a CRE cell to copy the creature file name (handy for NearInfinity's search).
   - **Gallery:** one card per creature with its portrait image, name, line count, race, gender and CRE file names (click a name to copy it). The cards fill the window width and re-flow when you resize it.
6. **Double-click** a creature (a gallery card or a table row) to open a window with its portrait, its lines (to get a feel for how it talks), and "Where this comes from" if the speaker needed explaining. **Copy all lines** puts the lines on the clipboard.
7. A text file named `<ModName>-creatures.txt` is saved next to the script automatically. Use **Save as text file...** or **Save as CSV...** to put it somewhere else.

Tick **Merge dialogs with the same creature name** if a creature shows up several times (some NPCs have a separate dialog for when they join the party). Leave it off if different creatures share a name, such as two different zombies; merging would add them into one row.

## Creature files and the bold names

Several creature files can use the same dialog (for example one NPC at different stages of the game), so the tool can't always tell which file speaks a line. A creature file is shown in **bold** (in `[brackets]` in the table) only when the mod itself ships that creature, names it in its own files, or the file has the same name as the dialog. If the mod gives no such clue, none is bold.

## Where this comes from

For a speaker the tool could not link to a creature, or only matched by name similarity, the lines window explains what is known: whether the dialog is in the override folder or the game's own archives, which creature files have similar names, where the mod's own files mention it (file and line), and which line of the mod led to it. The same notes are in the CSV's `notes` column. Usually this means the mod adds lines to a dialog that already exists, or a script attaches the dialog to a creature.

## Proposing portraits

On each gallery card press **Propose portrait...** (also in the lines window) and pick an image file. The card then shows that image with a thick green border and "Proposed: <file name>". Press **x** to remove it, **Clear all proposals** to remove all of them, or **Export proposed changes...** to write a to-do list:

```
# Portrait changes for alternatives  (2 creature(s))
B!ASTER.CRE  Aster -> BB12020.bmp   [now: none]
SKIE.CRE  Skie -> BB12021.bmp   [now: SKIEL]
```

A creature with several creature files gets one line per file. Proposals are remembered per mod in `portrait_scout.settings.json`, so they survive closing the window.

When you pick an image the tool checks it and warns (you can still use it) if:

- the name is too long or has characters a resref cannot contain (at most 8 characters);
- it is not a 24-bit BMP, which is what the game's portrait files are;
- its shape is not that of a standard portrait: 210x330 (large), 169x266 (medium) or 54x84 (small) are all fine.

The tool only proposes; it does not convert, rename or install anything. WeiDU also does not rename or convert images: the portrait files have to exist under the right names (for example `BB12020L.bmp`, `BB12020M.bmp`, `BB12020S.bmp`) and the creature file has to point at those names.

## Command line (no window)

```
python portrait_scout.py --cli "C:\Games\BG2EE" "C:\Games\BG2EE\MyMod" --out creatures.txt --csv creatures.csv
```

Add `--merge-names` to merge dialogs with the same creature name. Use the game folder as the mod folder for the whole install.

## What the columns mean

- **Creature**: the creature's name from the game text.
- **Lines**: how many of the mod's NPC dialog lines that creature speaks. The player's reply options are not counted.
- **Portrait**: the large portrait file name set on the creature. Shown as `(none)` when the creature has no portrait, `(file missing)` after the name when the file can't be found in the game, and `(creature not found)` when the speaker could not be traced to a creature.
- **Race / Gender / Animation**: taken from the creature file and the game's own `race.ids` and `animate.ids`, so the animation reads like "Thief Female Human" or "Fat Man".
- **CRE file(s)**: the creature file names, to search for in NearInfinity.
- **Proposed portrait** (table only): the image you proposed.

Below the table the text report also lists portraits used by more than one creature, and how many creatures have no portrait.

## How it finds the mod's lines

It reads the game's `chitin.key`, archives, override folder and `dialog.tlk` directly. Every string in the mod's source files (`.tra`, `.d`, `.baf`, `.tp2` and similar) is looked up in `dialog.tlk`, and the matches that are NPC lines in a dialog are counted for the creature that owns the dialog. Short stock lines such as "Yes." that every mod shares are only counted inside the mod's own dialogs. In whole-install mode every NPC line of every dialog is counted.

## Good to know

- **The mod must be installed in the game folder you pick.** If none of its text is in `dialog.tlk`, the tool says so instead of showing an empty list.
- It matches by text, so it is an accurate guide, not a perfect one. A speaker whose dialog cannot be linked to a creature shows as `Unknown (DIALOGNAME)`.
- The first run on a big install can take a minute or two because it reads every dialog and creature in the game.
- Only plain (uncompressed) game archives are read, which is what the Enhanced Editions use.
- The gallery shows up to 400 creatures; the table and text file list all of them.
- Remembered folders and proposals are saved in `portrait_scout.settings.json` next to the script. Delete it to reset them.
