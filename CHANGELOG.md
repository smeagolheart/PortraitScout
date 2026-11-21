# Portrait Scout - what changed

## 1.4a - 20 November 2026

### Features

- **Stretch**, on the Shape tab: pull a marked spot away from the middle of the face with a taper - a human ear
  drawn out to an elven point, a jaw lengthened, a horn pulled up. Minus pulls it back in.
- **Speckles**, on the Skin tab: freckles, a gith's markings, a pox-scarred face. Mixed from the skin's own
  color a few shades down. Click a cheek to put them somewhere of your own; **Area** sets how wide each patch is.
- **Match skin**, in the Spot color list: takes the face's own color instead of a named one, for an ear or a
  gem the engine painted a color that fights the face.
- Descriptions are shorter. Made-up faces, noses and marks are off by default (`face_detail` in
  `generator/settings.json`), and "adult" is said only when the age is not the default.
- Race terms lead with words the model holds: angelic human, devil-blooded human, shark-headed man, gaunt
  yellow-skinned humanoid.
- **bare_races** in `generator/settings.json`: creatures whose sprite wears nothing a portrait would show keep
  their race term and lose the class and clothing a creature file only borrowed.

### Changes

- Darker now darkens. It was lightening, which is not what the slider is called.
- Skin, Lips, Darker and Speckles no longer skip a strongly colored face. The mask asked whether a pixel was
  too colorful to be skin against a fixed ceiling, which a drow, a gith or a sirine passes straight over.
- A strength slider with no color chosen is grayed, rather than sitting there doing nothing.
- Ear phrases are gone from elf, half-elf and drow. The model paints its own ears whatever it is told.

## 1.4 - 20 November 2026

### Features

- **Touch up** a finished portrait, with a tab each for Skin, Lips, Spots, Shape and Picture. The engine paints
  what it recognizes whatever the description says, so this is done to the picture afterwards.
  - Skin color and lightness, lips, and a color for anything you click - an eye, a gem, a buckle.
  - A click lands on the tab you are on. Mark a spot and cut it: a healed scar or a fresh wound, with length, width, angle, how much light the edge
    catches and how dark a line is drawn round it. Wounds come in blood or ichor.
  - Light it: a glow or a flame in the shape of an eye, with light spilling onto the face, and a shadow setting
    for the pits an undead thing has where its eyes were.
  - Push a face out of shape: swell or pinch what you click - a broader nose, a heavier jaw.
  - Creases, grime and a ghostly fade, and mirror the picture so a roomful of portraits do not all face the
    same way.
  - Paint these in settles a pass and clears the marks, so the next scar can be set differently. Undo takes
    back a step or a whole pass.
- **Export work...** writes your pictures and what they are for into one zip, and **Import work...** reads it
  into another copy so someone else can carry on.
- **Ignore a creature** that will never want a portrait, with a tick box to list them again.
- **Stop** a batch without stopping the image engine.
- **Zoom** in the full-size view and in the Propose view.
- **Declone** now works when reading the mod's own files, not only an installed game.

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
- Colors go in right behind the race: at the end of a long description the model ignores them.
- Washed-out ramps read as silver or white hair rather than dyed blue.
- New face re-rolls for non-human creatures too, and Generate carries on past the seeds already used instead of
  handing back the same pictures.
- Proposals, ignores and decloned bunches survive the creatures being grouped differently - switching to Read
  Mod Files no longer empties the gallery.
- A creature with several dialogs says which dialog each line came from, and creature files and dialog files
  are listed apart.
- Imported pictures show up again when the Propose tab is reopened.

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
