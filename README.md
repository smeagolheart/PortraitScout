# Portrait Scout

Lists the speaking creatures of an **installed** Infinity Engine mod, or of a whole game install (BG1EE, SoD, BG2EE, EET, IWDEE, and classic BG1, BG2, IWD), with line counts, portraits and portrait proposals.

| Creature | Lines | Portrait | Race | Gender | Animation | CRE file(s) |
|---|---|---|---|---|---|---|
| Skie | 21 | SKIEL | Human | Female | Thief Female Human | SKIE |
| Urnn Indigospit | 11 | L#SSMAG | Human | Male | Mage Male Human | L#SSMAG |

![Portrait Scout gallery](screenshot.png)

Use it to plan NPC portraits: who talks, how much, which portrait each uses (spot duplicates and gaps), and which creature file to open in NearInfinity. It only **reads** your game folder and never changes it.

## Requirements

- Python 3.8+ with tkinter (included in the normal Windows/macOS installers; Linux: `python3-tk`).
- A game folder (contains `chitin.key`) with the mod installed via WeiDU, plus the mod's own folder (`.tp2`, `.tra`, `.d` files). Not IWD2 or Planescape: Torment.
- Optional: `pip install pillow` to preview JPG and other formats for proposed portraits (PNG, GIF and BMP work without it).

## Use

1. Copy `portrait_scout.py` into the game folder, open a command prompt there, run `python portrait_scout.py`. The game folder is pre-filled; the last one used is remembered.
2. Pick the **Mod folder**. Leave it empty, or pick the game folder, to list the whole install.
3. Press **Analyze** (can take a minute or two on a large install).
4. Switch **Table** / **Gallery** with the buttons beside it. Click table headers to sort. Clicking a CRE name copies it.
   In the gallery, scroll with the mouse wheel, **Up/Down**, **Page Up/Down** or **Ctrl+Home/End**. It shows 200 creatures per page: use **Previous/Next** or type a page number and press **Enter**.
5. **Double-click** a creature to see its portrait and lines (**Copy all lines** copies them) and, for speakers that needed explaining, "Where this comes from".
6. A `<ModName>-creatures.txt` report is saved next to the script. **Save as text file...** and **Save as CSV...** save elsewhere.

**Merge dialogs with the same creature name** combines separate dialogs of one NPC (e.g. a joined-party dialog). Leave it off if different creatures share a name, such as two zombies.

Portraits show their resolution everywhere. **Right-click** any portrait to save the image.

## Proposing portraits

Press **Propose portrait...** on a gallery card or in the lines window and pick an image. It shows with a green border; **x** removes it, **Clear all proposals** removes all. **Export proposed changes...** writes a to-do list:

```
B!ASTER.CRE  Aster -> BB12020.bmp   [now: none]
SKIE.CRE  Skie -> BB12021.bmp   [now: SKIEL]
```

One line per creature file. Proposals are remembered per mod.

The picked image is checked, with warnings you can ignore, for: a name over 8 characters or with characters a resref can't contain; not a BMP (24-bit or 8-bit are fine); an unusual shape (standard portraits are 210x330, 169x266 and 54x84).

Nothing is converted, renamed or installed, and WeiDU doesn't do that either: the files must exist under the right names (e.g. `BB12020L.bmp`, `M`, `S`) and the creature must point at them.

## Columns

- **Lines**: NPC lines the creature speaks (player replies excluded).
- **Portrait**: large portrait name, or `(none)`, `(file missing)`, `(creature not found)`.
- **Race / Gender / Animation**: from the creature file and the game's `race.ids` / `animate.ids`.
- **CRE file(s)**: creature files sharing the dialog. **Bold** (in `[brackets]` in the table) means the mod ships, names, or matches the name of that file; no clue means none is bold.
- **Proposed portrait**: your proposal (table only).

The text report also lists portraits shared by several creatures and how many have none.

## Command line

```
python portrait_scout.py --cli "C:\Games\BG2EE" "C:\Games\BG2EE\MyMod" --out creatures.txt --csv creatures.csv
```

Add `--merge-names` to merge same-named creatures. Use the game folder as the mod folder for the whole install.

## How it works

It reads `chitin.key`, the archives, override and `dialog.tlk` directly. Every string in the mod's `.tra`, `.d`, `.baf`, `.tp2` and similar files is looked up in `dialog.tlk`; matches that are NPC lines are counted for the creature owning the dialog. Short stock lines ("Yes.") are only counted inside the mod's own dialogs. Whole-install mode counts every NPC line.

## Notes

- The mod must be installed in the chosen game folder, or the tool says so.
- Matching is by text: accurate, not perfect. Unlinked speakers show as `Unknown (DIALOGNAME)`, and the lines window and CSV `notes` column explain what is known.
- Only plain (uncompressed) archives are read, as in the Enhanced Editions.
- The gallery pages 200 creatures at a time and always shows at least 4 per row (a narrow window gets a sideways scrollbar). The table and text file show all creatures.
- Folders and proposals are stored in `portrait_scout.settings.json` next to the script; delete it to reset.
