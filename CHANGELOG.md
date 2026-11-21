# Portrait Scout - what changed

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
