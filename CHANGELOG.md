# Portrait Scout - what changed

## 1.5 - 22 November 2026

### Features

- **Missing picture(s)...** appears on the Proposals tab when a creature is proposed a picture that is no
  longer on the disk. It points each one at another crop of the same picture where there is one, and lists
  the rest. A re-crop deletes the crop it replaces, so a proposal could be left pointing at a deleted name.
- **Create mod...** writes in the creatures a mod builds by copying one of the game's own, with the face that
  creature wears. A second Bassilus is still Bassilus, so the copy keeps looking like it whatever order the
  mods were installed in. The face is named, not copied, and a creature whose face is not in that game is
  left alone; one box carries the pictures along instead, for a mod that has to work without whatever
  supplied them, and the readme then names the faces it does not carry, since they are somebody else's work.
  - **Everything I see** does the lot: the mod puts those creatures back the way they look on your game,
    pictures and all. Untick it and the two halves of that are there to set one by one - whether copies of a
    stock farmer or merchant are included at all, and whether the pictures are carried or only named. A stock
    face is the doubtful one: that picture is whatever each install puts there, and need not even be the
    same sex.
- A creature that wears a face only because it was copied from somebody is marked whether or not the copying
  worked. The install order decides whether it shows that face or nothing at all, and only the second was
  ever obvious - Cedric Shan has been wearing Kivan's portrait and nothing said so.
- Creatures a mod builds by **copying one of the game's own** are read as such. A copy keeps the original's
  face, so install a mod before whatever gave that original its portrait and the copy is left with nothing.
  Analyze now reads the mod's installer for those copies and shows the borrowed face, marked as borrowed, with
  the Portrait column reading `(none) <- FARM` and the creature's window naming the file it came from. The
  same face turning up a dozen times is where a portrait of its own is wanted most.
- **Touch up** from the picture menu: right-click a portrait on a gallery card, in a creature's window or in
  the full-size view. It works on the picture the creature has now, and what comes out becomes the proposal.
  Until now Touch up could only be reached from a candidate in the Propose tab.

### Changes

- **Party NPCs** finds the ones an EET game keeps out of its dialog tables. `pdialog.2da` can hold only the
  BG2 NPCs, which left Kivan, Khalid, Alora, Skie, Tiax and the rest listed with everybody else; the sizes
  named in `portrait.2da` find them. Only the name of the picture's owner is read, never the picture, so a
  creature merely wearing a party NPC's face is still listed.
- Buttons and tick boxes say it in three words or fewer, with the explaining left to the tooltip, and no
  tooltip runs past about forty words.
- A touch up no longer deletes the picture it was made from. **Plain** had nothing to go back to, and because
  the plain picture was gone the next touch up started from the touched one and piled on top of it - which is
  where `_touch_touch` and `_touch_touch_touch` files came from.
- **Discard** works on the whole family, so the plain picture kept above is not left behind.
- **Import work...** no longer copies the pictures it is about to ignore. Answering "keep yours" still wrote
  every picture in the bundle to the disk - 105 of them, 14 MB, that nothing then pointed at.
- A proposed picture that has been deleted says so on its gallery card, instead of claiming the format cannot
  be previewed.
- Shared work lands in the folder the generator settings name. `output_folder` was read through a variable
  that was never in scope, and the error was swallowed, so imported work always went to `generated/`.
- "How many" fits in the Skin tab again instead of reading "How man".

## 1.4 - 20 November 2026

### Features

- **Touch up** a finished portrait, with a tab each for Skin, Lips, Spots, Shape and Picture. The engine paints
  what it recognizes whatever the description says, so this is done to the picture afterwards. It works on a
  generated candidate, on a portrait you have already proposed, and on the one the creature wears in the game -
  that one now joins the candidates in Propose, so a stock face can be worked on and proposed back.
  - Skin color and lightness, lips, and a color for anything you click - an eye, a gem, a buckle. Colors are
    moved from what is already there, and a strongly colored face - a drow, a gith, a sirine - is read as skin
    rather than passed over.
  - **Speckles**: freckles, a gith's markings, a pox-scarred face, mixed from the skin's own color. How many
    there are, how hard they are painted, whether they sit lighter or darker than the skin, and how wide each
    patch is. Click a cheek to put them somewhere of your own.
  - A click lands on the tab you are on. Mark a spot and cut it: a healed scar or a fresh wound, with length,
    width, angle, how much light the edge catches and how dark a line is drawn round it. Wounds come in blood
    or ichor; a scar is mixed from the skin's own color where it falls, so it reads on a dragon as well as on
    a farmhand.
  - **Light it**: a glow or a flame in the shape of an eye, with light spilling onto the brow and cheekbone,
    and a shadow setting for the pits an undead thing has where its eyes were.
  - Push a face out of shape: swell or pinch what you click - a broader nose, a heavier jaw - or **stretch** a
    marked spot away from the middle with a taper: a human ear drawn out to an elven point, a horn pulled up.
  - **Match skin**, in the spot color list, takes the face's own color instead of a named one, for an ear or a
    gem the engine painted a color that fights the face.
  - Creases, grime and a ghostly fade, and mirror the picture so a roomful of portraits do not all face the
    same way.
  - Its own zoom, so a 169x266 game portrait is not worked on at stamp size. Clicks and the marks you place
    scale with it; the pass itself always runs at the picture's own size.
  - **Paint these in** settles a pass and clears the marks, so the next scar can be set differently. **Undo**
    takes back a step or a whole pass.
  - A picture that is already portrait-sized comes back the same size, with nothing cut out of it a second
    time, so Propose under it makes it the creature's portrait as it stands.
- **Export work...** writes your pictures and what they are for into one zip, and **Import work...** reads it
  into another copy so someone else can carry on.
- **Ignore a creature** that will never want a portrait, with a tick box to list them again.
- **Stop** a batch without stopping the image engine.
- **Zoom** in the full-size view and in the Propose view, with Touch up offered from the full-size view.
- **Declone** now works when reading the mod's own files, not only an installed game.
- **bare_races** in `generator/settings.json`: creatures whose sprite wears nothing a portrait would show keep
  their race term and lose the class and clothing a creature file only borrowed.
- `test_portrait_scout.py`: 17 tests over the parts that run without a game folder, a window or an image
  engine - the .CRE struct offsets and both readers, the WeiDU `.d` parser, text normalizing, the color words
  a palette turns into, the mod-export helpers, and cutting a picture down to a game portrait.

### Changes

- Creature colors are rolled when the creature is spawned, not stored in its file. They are read as the sets
  they are, so a drow is no longer described with yellow skin.
- Improved creature detection scripts.
  - A creature's race is worked out from the best evidence there is, rather than a field that is often just
    "human". Half-elves are no longer elves, half-orcs are no longer monsters, and the Shadow Thieves are no
    longer living shadows.
  - Undead and other non-humans get descriptions of their own, and skip the colors they have no skin or hair
    for.
  - Hobgoblins, ogres and half-ogres wear the helms their sprites never take off.
- Descriptions are shorter. Made-up faces, noses and marks are off by default (`face_detail` in
  `generator/settings.json`), and "adult" is said only when the age is not the default.
- Race terms lead with words the model holds: angelic human, devil-blooded human, shark-headed man, gaunt
  yellow-skinned humanoid.
- Ear phrases are gone from elf, half-elf and drow. The model paints its own ears whatever it is told.
- Colors go in right behind the race: at the end of a long description the model ignores them.
- Washed-out ramps read as silver or white hair rather than dyed blue.
- New face re-rolls for non-human creatures too, and Generate carries on past the seeds already used instead of
  handing back the same pictures.
- Proposals, ignores and decloned bunches survive the creatures being grouped differently - switching to Read
  Mod Files no longer empties the gallery.
- A creature with several dialogs says which dialog each line came from, and creature files and dialog files
  are listed apart.
- Imported pictures show up again when the Propose tab is reopened.
- **Use this** only replaces the .bmp files it cut out itself. A picture you brought in, or a portrait you
  touched up, stayed on the window but was taken off the disk.

## 1.3d - 18 November 2026

A description stays with its creature rather than drifting to another; wider feature lists for faces; pictures
imported from a file; a face drawn per creature rather than per portrait.

## 1.3c - 17 November 2026

Proposing in bulk from a folder of pictures; one way to choose a portrait throughout; creatures the installer
builds out of a game creature are read.

## 1.2 - 15 November 2026

Reading the mod's own files before it is installed; the areas a creature appears in; the portrait generator; mod
export as an installable WeiDU mod.

Earlier versions are in the git history.
