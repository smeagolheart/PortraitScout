# Portrait Scout - what changed

## 2.0 - 27 November 2026

### Features

- **Generator setup** at the top right opens the setup steps for the image engine, with their download links. The first time Portrait Scout opens with no engine, they open by themselves.
- **The first start of the engine says so.** Forge installs its packages the first time it runs, which takes a while. The status line now says "First start, setting up - please wait..." with the time so far, and waits up to an hour instead of giving up at 10 minutes. Later starts are the usual minute or so.
- **Propose: the engine line follows along.** It said "not running" all through an engine started by Generate; it now says starting..., then running. While Forge loads the model it reported a stuck "1%" - it now says "loading the model... 95 s", and past 45 seconds that the first load reads the whole model from disk.
- **Portrait defaults in Generator setup:** the Scars and Grime a new portrait gets when you pick none (Scars was always Few, a thin healed scar on one cheek; now it can be None). Grime has a default of its own now too (`grime_default`, Clean).
- **A taller window to start with,** so a row of candidates and their buttons fits without scrolling (up to 820 pixels, less on a small screen).
- **Download faces** in the setup window fetches the face bank (about 550 MB, synthetic faces from SFHQ, MIT License) from the latest release and unpacks it into the faces folder, with a progress bar; faces already there are kept. The setup window opens by itself the first time Portrait Scout starts without an engine or without faces.
- **Setting up Forge Neo is easier.** *Run it* opens a command window in the right folder and runs the clone and the venv commands. *Model*, *Encoder* and *VAE* download each file directly (sizes corrected: 9.4 GB, 8.7 GB, 336 MB). *Use Forge Neo* writes the engine, model, encoder, VAE, 4 steps, CFG 1 and the Klein painterly style into generator/settings.json, with this computer's paths. *Run it* windows say whether the command worked and close on a key press, and the step ticks as soon as the window closes. *Move downloads* moves the model files from your Downloads folder into their folders once they have finished downloading, in the background, with the file it is on and a moving bar, and says which files are not in Downloads yet; each file also has an *Open folder* of its own. *Use Forge Neo* then starts the engine for its slow first start (Forge installing its packages) right there in the setup window, with its own status line and a moving bar, so the first Generate does not wait for it. The steps' text can be selected and copied; right-click > Copy all steps copies the whole window. The setup window opens as big as its steps, so nothing needs scrolling on a normal screen. *Check files* ticks each step that is done, and says what is missing: git or uv, the clone, its Python, a model file that is still downloading or in the wrong folder, or settings that do not point at it yet.
- **Preview page.** Proposals > Preview page... saves one web page of the proposed portraits (game size, each with its name and race and gender), to look the set over in a browser or share it: a search box, click a portrait to see it bigger. The pictures are inside the page, so it is a single file. Create mod... puts one in the mod too, as preview.html. *Export work...* puts it in the work's .zip too (preview.html), for a look at an unfinished set at a glance.
- **Race filter.** A Race list beside the gallery's filters shows one race at a time (goblins, dwarves...) in the gallery and the Expanded view.
- **Pick from the pool: tick and delete.** The pool window in Propose and Expanded has a tick under each picture, Tick all, and Delete ticked, besides right-click Delete. And the keyboard: arrows (and Page Up/Down, Home, End) move a blue frame, Space ticks and moves on, Enter uses the framed picture, Delete deletes the ticked ones (or the framed one).
- **Child faces.** faces/child/male and faces/child/female hold 122 children (63 boys, 59 girls) picked from SFHQ part 4 (a probe on its CLIP features found the children; the ones kept were picked by eye) and lined up like the rest of the bank. A child (the Boy and Girl animations, or Age: Child) takes its face from there. Before, "child male human" was not recognized as a child, and children took grown-ups' faces from the bank. A child's photo is cut at the chin too, so its bright T-shirt does not come back as a pale band.
- **Random colors, the same in the sprite and the portrait:** a color in a creature file that names a random set in RANDCOLR.2DA (OgreSkin, DrowMajor...) is rolled by the game when it makes the creature. Portrait Scout now rolls it once per creature, the same way every time (and again when the roll is itself a set, as OgreSkin's often are), and both the portrait description and the sprite view use that roll. Before, the description said nothing and Near Infinity rolled its own, so the sprite showed a color the portrait never got. The sprite view lists the random slots under the sprite ("Random in game: skin, major, minor"), with the sets' names in the tooltip. Gold skin is worded "sallow yellow skin". Analyze again to pick this up; sprites drawn by the old helper are drawn again.
- **Monsters use the face bank too, and each gets a face of its own.** Orcs, goblins, hobgoblins, half-ogres, xvarts and the like are painted in two passes. The first paints the bank face lightly (`face_bank.monster`: strength 0.74, 8 steps, no blur), so it keeps the photo's own face. A blurred start picture stayed blurred through both passes. Painted further, every half-ogre came back as the same face. The second pass (strength 0.85, `edit`) asks the model to turn that face into the creature, with its jutting brow, tusks and skin, to paint the neck, shoulders and clothes sharply (the seam under a photo's chin otherwise stayed as haze), and to keep everything else. The words for each kind are `monster_edits` in generator/settings.json, and `{skin}` is the creature's own skin. Kobolds, beasts and the undead still start from nothing.
- **Ogres and ogre mages: a small head on a huge body.** Like their sprites and the old artwork: the bank face small and high over a huge body shape in its skin color - broader or narrower, more or less belly, a vest or an ogre mage's robes - painted at 0.85 (`hulk_strength`), so every ogre is built a little differently. Ogre mages keep their short horns and high ponytail, and take their skin color from their file like ogres. Half-ogres and ogrillons (helmeted, less bulky) keep the head-and-shoulders portrait.
- **Dwarves use the face bank, with long beards.** Dwarves were left out of the bank because a photo's chin kept the beard short. Now when the words ask for a long beard, the photo's chin and neck are painted over with a long beard shape in the hair's color before the picture starts, and it comes out down the chest. Male dwarves' beards are all long now (full, braided, ring-bound or forked, halfway down the chest). A beard in the words no longer keeps any creature from the bank. The bank photo's edge is also blended further, for no seam under the chin.
- **More creatures use the face bank.** Bare chests and shoulders (the Beggar Man) and ponytails (the Harlot Woman) no longer keep the bank out: a bare chest is painted as skin from under the chin down. A creature whose file names a beast race but who walks about, and is described, as a person (a lycanthrope on a human sprite) uses it too. Gnomes use it too (and only gnomes: a human's "long, thin nose" from the face words is the photo's to keep): when the words ask for a long, hooked, crooked or pointed nose, the photo's own nose is made half again as long before the picture starts, and big pointed ears are painted on, so a gnome keeps a gnome's nose and ears with a face of its own. Monsters (orcs, goblins, ogres, xvarts...) still do not - a person's face makes them human. A bald head in the words (the Fat Man, the Beggar Man) used to keep the bank's photos out too, because the photo's hair won. The photo's hair is now painted over with a bare scalp in its own skin color, and for very fat or obese words the cheeks, jowls and chin are filled out round the photo's own eyes, nose and mouth (only then: for plump, a double chin or a big round nose - a halfling's - covering the photo's cheeks or stretching its nose gave every halfling one face). The bank face's own frame is blended away too (it could leave a box round the head).
- **Xvarts:** a race of their own. The Kobold Xvart animation was described as a kobold (a scaly lizard snout); it is now a small wiry blue-skinned goblin-like humanoid with a bald domed head ringed with dark hair at the back and sides, large pointed ears, orange eyes, a sleeveless brown leather jerkin and bare blue arms, with the monster variety (age, face, temper, marks).

### Changes

- **"Neither" is no longer shown** for a creature with no sex set: the Table, the gallery captions, the Propose header, the text and CSV exports and the preview page show just its race ("Ogre", not "Ogre Neither").
- **Ogre looks like its sprite:** bald and bare-headed (the horned helmet is only the Half Ogre's), a crude sleeveless leather vest. Its skin is the creature's own skin color from its file (red for one, sallow yellow for another), and the ogre race no longer picks a random skin at all - Ogre, Half Ogre and Ogrillon all take the creature's own skin color. Other sprites can opt in with `"skin_colors": true` in sprite_terms.
- **Animals wear no clothes:** a wolf, bear or other animal with no race line of its own was described as a man would be once its file had a gender - "adult male wolf ... a plain faded dark teal wool tunic", its sprite's colors read as clothes. It is now just the animal ("wolf", "winter wolf", "black bear").
- **Children's bodies.** A child's start picture had a grown-up's shoulders under it (the shoulder fill spans the frame, and the body paintings are adults), so children came out with adult bodies. A child's fill is now narrow shoulders (the body painting's folds on them; without them the fill came back as blur), and the child lead adds "a small slight child's body with narrow little shoulders and a thin neck".
- **Bald heads from the bank: no blurred patch.** The bare scalp painted over a photo's hair was a flat, smooth patch, and came back as a blur on the head. It is now laid only where the photo has hair (a head already bald keeps its own scalp), with the forehead's own skin texture on it.
- **No more hazy backgrounds from the face bank.** The background around a bank face was a plain blur of the photo, and the painting kept it as haze. It now carries the light and brush strokes of one of eight painted backdrops (faces/_backdrops), in the photo's colors, and comes back as a painted background.
- **Touch up:** opens with Color it on Leave every time (a remembered or guessed skin color was laid on at once), and has a Change it... button and a Mirror it tick on its first tab. Change it... keeps the touch up done so far (saved as the candidate) and changes that picture, not the one Touch up started from.
- **Children's start pictures.** A child's face is painted a little bigger (0.9), the photo cut at the chin, the shoulders across the frame with a boy's plain tunic folds (narrow ones left the sides blurred) (a woman's bodice painting gave girls a grown woman's figure), and the background beside them blurred from above the shoulders only (the photo's pale shirt had smeared into it).
- **Expanded: a creature stays after its spares are deleted.** Discarding every picture but the one proposed (in Propose) took the creature off the Expanded view; one with a proposal stays.
- **Children are just children, not female and male.** A child was described "child female human", and "female" painted a grown woman's body under a child's face. It is now just "child human" / "child elf"; the creature's gender picks the boy or girl face from the child bank.
- **Goblins are goblins again.** A goblin's file takes its random colors from the kobold sets (KoboldSkin...), and that made it a kobold: a scaly lizard snout and horns. A creature's color sets now only change its race when the race field cannot say it (a drow filed as an elf). Goblins have a line of their own too: small and scrawny, large pointed or batlike ears, sallow yellow to light green skin, a hooked, pointed, snub or warty nose - and the face bank like the other monsters.
- **Pool: no sprites.** The creatures' sprites (generated/_sprites, drawn by Near Infinity for the sprite view) were counted as spare pictures - thousands of them. They are left out of the Pool now. (Any you deleted are drawn again the next time a creature's sprite is shown.)
- **Changing Age, Scars, Look and the like no longer loses the sprite's look.** Any change in the Built from lists rebuilt the description from a class-and-race name ("Beggar Male Human") instead of the creature's own animation ("Beggar Man"), so the Beggar Man lost his bald head, beard and bare chest. With race, gender and role as the creature has them, its own animation is used.
- **Age "Adult" adds no word.** "adult" led every description built at the default age and made beasts more human; Adult is what gets painted anyway. Young, Old and the rest still say their words.
- **Face bank: heads the size of the rest.** A bank face filled more of the frame than a picture painted from nothing, so portraits started from it had big heads. The face is now painted at 0.8 of its size, eyes in the same place, with more shoulders below (`face_bank.scale` in settings.json).
- **Face bank: no more washed-out bottoms or faces in a blurry circle.** The bank's faces end at the chin, with a smooth blur below and often around them, and a picture started from a blur kept it. The blur is now painted over before each picture: a dark shoulders-and-neck shape in the collar's color, with the light and dark of a painted head-and-shoulders picture over it (faces/_bodies, four men and four women, of the face's gender) - real folds and straps come back as clothes in the described colors. Only the shoulders take the painting's shapes, toned down; around the head the fill stays a soft blur, so the background is the one Background asks for rather than the painting's arches and walls. A soft fill came back as blur along the bottom, and a made-up texture as blocks.
- **Noble Woman looks like its sprite:** a tall two-peaked cloth headdress (an escoffion) with a sheer veil, long hair hanging behind it, a wide collar over the shoulders. Worded without "horned", which painted Viking helmets.
- **Noble Man looks like its sprite:** a floppy velvet Renaissance beret tilted to one side, with a long curling plume feather.


## 1.5 - 22 November 2026

### Features

- The class picker on the Propose tab also offers who someone is: Commoner, Peasant, Poor, Beggar, Farmer,
  Laborer, Servant, Innkeeper, Smith, Sailor, Merchant, Guard, Soldier, Priest, Scholar, Noble, Royal. Each has
  its words in `animation_terms` ("innkeeper, an apron over plain clothes"); Royal dresses fine like Noble.
- **Vary faces** beside Candidates (on by default): each picture of a Generate gets its own face in words -
  age, face, nose, mouth, eyes, hair, sometimes a mark - drawn from `vary_terms` in generator/settings.json.
  Klein paints one face per description whatever the seed; four seeds of the same words were the same woman,
  four drawn faces were four different women. Whatever the description already says (age, hair color, eyes)
  is not drawn. *Use this face* puts the picture's face words back in the description with its seed.
- **LoRA strength** beside the LoRA tick box on the Propose tab (Style's, 1.0 down to 0.2), remembered. The
  portrait LoRA paints armor and an adventurer's coat whatever clothes are asked for; at 0.35 rags come out as
  rags. Cloth gains Rags.
- **Cloth** picker on the Built from row: Wool, Linen, Leather, Silk, Velvet, Fur or Plain (the color only),
  remembered per creature. The materials are `cloth_terms` in generator/settings.json, and the sentence they go
  in is `clothes_worn` / `clothes_fine`, so the wording can be changed there too.
- **Seed** on the Propose tab: right-click a picture and *Use this face* (or *Copy seed*) - read from what the
  engine wrote into the PNG, also for a touched-up copy or a game-size .bmp. With a seed set the first picture
  starts from that face, so changing the description keeps the face; empty, every Generate is somebody new.
  Remembered per creature. It replaces **New face**, which did nothing with face_detail off.
- **Touch up: Erase.** Tick Erase and click or drag on the picture to rub out what this pass did there -
  speckles that strayed off the face, dirt in the hair - with a Size for the eraser. Undo takes back a stroke.
- **Touch up: Paint tab** - face paint in six patterns (cheek stripes, eye band, slash, center line, dots,
  chin lines) and seven colors (woad blue, red ochre, black, white, green, yellow ochre, ash), with Strength
  and Size. Click the two eyes and the paint is placed from them; it is dry-brushed at the edges, lets the
  skin's texture through and stays off the whites of the eyes.
- **Touch up: Dirty it** on the Picture tab - Soot, Mud, Blood (dried spatter) and Sweat, each with its own
  strength, and a Size for the smudges. Like Grime they stay on the skin.
- **Discard ticked** in the Propose tab: every candidate has a tick box (or Ctrl+click the picture), and the
  button deletes the ticked ones in one go, after asking.
- **Borrowed face** sort in the gallery: creatures wearing a face copied from another creature come first,
  grouped by that face, most lines first.
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
- **Pool**: a tab of every picture made and not used, across every mod analyzed, newest of each family -
  165 of them here after a week's generating. Tick some and hand them to creatures you pick: one picture goes
  to all of them, several are dealt out in order, so twenty commoners can be given twenty faces at once. Tick
  and Delete the ones that came out wrong. Search narrows by creature, file or mod, and two pickers
  narrow by race and sex - a picture's race and sex are those of the creature it was made for, which is
  who is in it.
- **Groups in the pool picker**: the creature list opens on the ones with no face yet, and a Group picker
  offers each set a mod copied from the same creature - `Copied from FARM (11)`, `Copied from HALFG3 (20)`
  - because creatures sharing a template are usually the same kind of person, so one batch of faces suits
  all of them. Picking a group ticks it. **Reuse pictures** off means each picture goes to one creature
  and the rest are left alone; on means the pictures go round again.
- **Right-click a spare picture** for what to do with just that one: use it for a creature, touch it up
  (what comes out is a new spare beside it, so the one you had is kept), save a copy, open the folder,
  tick it, delete it. With several ticked the menu offers those as well.
- **Select image... from the pool**: on the Propose tab the button now offers *From a file...* or
  *From the pool...*, which shows the spares in a grid - narrowed, for a creature, to the ones made for
  the same race and sex. Click one and it joins the candidates to be framed like any other picture, so
  a spare half-orc face is three clicks from being the next half-orc's portrait.
  A spare used this way leaves the pool, even though what gets framed is a copy of it, and
  **Delete the spare** throws the file away as well.
- **Show only:** on the gallery - no portrait, proposed, missing, borrowed, 10+ lines. Tick as many as you
  like and a creature has to match them all, so "borrowed" with "proposed" off is the list of creatures still
  wearing somebody else's face.
- The Propose tab opens with a strip showing the portrait the creature has in the game beside the one being
  proposed, and where the description came from.
- A gallery card says in a line what it is - proposed, missing file, ignored.
- **Touch up** from the picture menu: right-click a portrait on a gallery card, in a creature's window or in
  the full-size view. It works on the picture the creature has now, and what comes out becomes the proposal.
  Until now Touch up could only be reached from a candidate in the Propose tab.

- **Keep face**: a tick on every creature wearing a borrowed face, saying whether that face is the right
  one. Ticked, Create mod writes it into the creature so it survives whatever order the mods install in;
  unticked, the creature wants a portrait of its own and nothing is written in. The tick starts on
  Analyze's guess and is yours to correct - the guess calls a template "the same creature" when the
  name matched *or* it was copied only once, and 258 of CotSC's templates are copied once, so Shandalar
  (right) and Shalvey's generic beggar (wrong) both pass it. It is on the gallery card, in the creature's
  window and in both right-click menus, with a **Face kept** filter to work through them. Create mod is
  unchanged until you tick something: with nothing ticked it writes in exactly what it wrote before, and
  its second box still reads **Stock faces too**. Once a mod has ticks, that box becomes **The rest
  too**, meaning the ones you left unticked.
- **Zoom 1** button beside Mid on the crop row, which puts Head zoom back to 1.0.
- **Forge Neo and FLUX.2 Klein 9B.** `generator/start_engine_neo.bat` starts Forge Neo from an
  `engine-neo` folder with its API on, and passes `--forge-ref-a1111-home ..\engine\webui`, so the old
  engine's models, LoRAs, VAE and text encoders are read as well and nothing has to be moved. The setup
  panel on the Propose tab now has both engines to pick between, each with its own steps, download
  buttons and folders, and `generator/README.md` has the model files, where they go, and the
  `settings.json` for each. A **Klein painterly** style ships with it, carrying no LoRA on purpose - the
  painterly LoRA has a face baked into it, so every seed painted the same person. Klein is
  step-distilled: 4 steps at CFG 1.
- An engine in `engine-neo` counts as an engine: Portrait Scout looked only in `engine\webui` before, so
  someone who had installed only Forge Neo was told there was no generator.

### Changes

- **Pool: sort, tick boxes, Tick all, Hide Auto's.** Sort by Newest (default), Oldest, Creature or Folder, remembered. Each picture has a tick box under it. *Tick all* ticks everything listed on every page (as narrowed by search and the pickers), so a whole batch of leftovers goes with one Delete. *Hide Auto's* (on) leaves out pictures still on an Auto card whose pick is not applied yet, so they cannot be deleted by accident; deleting a picture also takes it off its Auto card.
- **The creature's sprite beside its portrait.** The lines window (double-click a card) shows the creature as it stands in the game, in its own colors - the first direction of its standing animation, as Near Infinity's Animation tab shows it, drawn by Near Infinity's own decoder. Needs Java and NearInfinity.jar in the Portrait Scout folder; `generator/spriteshot.jar` (source beside it) does the drawing. The first one takes a few seconds while Near Infinity opens the game; then the rest of the mod's creatures are drawn in the background, and every sprite is kept in `generated/_sprites`. Right-click a sprite to draw it again. Every direction is drawn: the arrows under it turn it, as Near Infinity's cycle buttons do. It is kept to a 200x230 box (shrunk for a dragon, enlarged in whole steps for a squirrel); click it for a bigger view, with scroll bars when it is larger than the screen.
- The lines window no longer widens for a long proposed file name.
- **Beggar Man looks like his sprite:** bald, a straggly beard, bare-chested (`sprite_terms`). Seen in the new sprite view.
- **Touch up moves to the right-click menu** of a candidate (and stays in the full-size view and the pool's menu). *Change it...* does most of what it did, with more control; Touch up is kept for what the edit model will not do reliably - glowing or burning eyes, an exact skin or eye color.
- The gallery's **Auto** tick box is now called **Expanded** (each creature's card opened out, with its new pictures, crop and batch tools).
- **Orog looks like its sprite:** bald, a grotesquely long horse-like face with a huge jaw, one pair of huge pointed ears set high and sweeping straight up past the crown, dull reddish-orange skin, black spiked iron shoulder plates over a dark leather harness. A sprite's own skin, ears and face replace the race's (the orc line's green skin and pointed ears), and when the sprite gives a long face, the random face shape is picked from long ones (gaunt, jowly, warty, craggy...), so orogs still differ. Ear marks and earrings stay out when the ears sit high, so no second pair is drawn. A sprite that names a skin now replaces the race's own (an orog is not orc-green), and a bald sprite drops the race's hair.
- **Ogre Mage looks like its sprite, not an ogre's:** reddish-brown skin, a bare hairless head with two short curved horns, layered white and gray robes with pale shoulder pieces - no horned helmet. A race pick (skin, nose...) now steps aside when the sprite already says that part, so the sprite's skin is not contradicted by a random one.
- **Harlot Woman too:** hair up in a high ponytail, an off-the-shoulder medieval linen chemise under a front-laced bodice (instead of the role's velvet gown with sleeves).
- **The face bank stays out of bald, bearded or bare-chested pictures.** A photo's hair, shirt and clean chin won over the words (a combed, clean-shaven beggar in a shirt), so a description that asks for any of them is painted from the words alone.
- **The sprite on the Propose tab too**, in a panel at the top right beside the rows (header to status line), with the pool spares next to it, both bigger than before: the sprite up to 150x200 with its arrows under it, six spares in two rows of three, *More...* under them.
- **A proposal whose picture is gone still leads the candidates.** Zexylinn's, brought in long ago, has only its 169x266 cut left (and no zoom mark in the name, which the search for its picture needed): that cut is now shown first, green-framed and marked "proposed now".
- **Change it... has a Strength slider** (0.50-1.00 in steps of 0.05, click or drag anywhere on it, remembered with the last change typed). 1.00 makes the change in full; lower blends it with the picture as it was (painted in 8 steps so there is room between): "a little darker" that came out near black is about right at 0.7.
- **Propose: no pictures in the header.** The In-game and Proposed thumbnails at the top are gone (both pictures are among the candidates below, the in-game one as "its portrait now", the proposed one in its green frame), which gives the candidates their height back. The header keeps the file names, and the proposed name now updates when a new one is proposed.
- **Discard all keeps the proposed picture.** Everything else for the creature goes; to delete the proposed one, right-click it > Discard.
- **Propose: the proposed picture leads the candidates.** The picture the current proposal was cut from comes first among the candidates, marked "proposed now" in a green frame, so it can be cropped, edited and right-clicked like the rest. Edited pictures (_e) and ones taken from the pool (_p) are listed again when a creature is reopened; before, only generated and brought-in ones were.
- **Propose: the proposal follows the crop.** Changing Zoom or nudging the box after *Use this* cuts the proposed portrait again from its picture (when that picture is one of the candidates shown), instead of leaving the old cut until *Use this* is pressed again.
- **Copies that keep their face show it with Mod's portraits only on.** Shandalar, Bassilus, Valas De'Vir and the like (Keep face ticked) wear the portrait of the creature they were copied from; with *Mod's portraits only* on they showed "no portrait from this mod", as if they had lost it. They now show that portrait, marked "inherits NAME", and the *Proposed* filter lists them with the proposed ones, since their portrait is settled too.
  The game's own creatures that the mod only adds lines to (Thalantyr) likewise show the portrait the game gives them, marked "game's own (NAME)".
- **The Auto view follows the search box and the filters**, as the gallery does: searching "daltan" shows Daltan's card, not all 293.
- **Pool > Open in Propose** works right after starting Portrait Scout (it analyzes the mod first), and finds a creature merged into another's row.
- **Pool > right-click > Open in Propose:** opens the creature the picture was made for on the Propose tab, with the picture already among its candidates (taken out of the pool once it is proposed).
- **Change expression / Change it...** Right-click a candidate on the Propose tab: *Change expression* > Neutral, Smiling, Stern, Sad, Anxious, Tired, Smug, Surprised or Angry, *Other...* for an expression in your own words, or *Change it...* for any change in words ("make the hair gray", "add a deep hood"). Klein repaints the picture from itself in edit mode, so the person, clothes, pose, background and style stay and only that changes; as many new candidates as Candidates says. (Plain img2img could not do this: at any strength that kept the face, the smile stayed.) The list and the words are `edit_terms` in generator/settings.json.
- **Tooltips no longer take the keyboard.** Each tooltip put the keyboard on itself (the way a dialog takes it so Esc closes it), so once it closed the keys went nowhere: the Pool's arrow keys, Space and Delete did nothing after hovering a picture, and a tooltip popping up while typing could swallow keystrokes anywhere.
- **Pool keys.** Arrow keys move a blue highlight along the pictures (the page scrolls to follow), Space ticks or unticks the highlighted one, Home / End jump to the first and last on the page, Page Up / Page Down turn the page, and Delete deletes the ticked ones (it asks first). Clicking a picture moves the highlight there. Keys typed into the search box are left alone. The keys stay with the pictures after a click on a button (Space used to press the Pool button again, redrawing the page and losing your place), and reading the folder again keeps your ticks.
- **Pool: ticking no longer redraws the page.** Each tick (and Tick page, Tick all, Untick all) redrew all 200 pictures, and a blank block flashed in the corner while they were laid out again; now only the tick marks change.
- **Pool pages like the gallery's:** 200 pictures to a page, with *< Previous* / *Page [n] of N* / *Next >* at the top and the bottom; type a page number and press Enter. The small < > buttons, which the wider toolbar pushed out of sight, are gone.
- **No more brick walls.** The {random} background palette "dull brick red and dark umber" went into the background words, and the model painted a brick wall. Palettes now name colors only: rust red, pale gray (was pale stone), pale cream and soft tan (was aged canvas and parchment), terracotta (was clay).
- **Apply picks no longer undoes a choice made in Propose.** A creature proposed in the Propose tab after its card was picked (or set to Keep by default) kept that old pick, and Apply picks put it back over what you chose in Propose. Choosing in Propose now takes the card's pick away, on every page.
- **A slight turn of the head, now and then.** About 3 in 10 pictures get a slight turn left or right, a slight tilt, or eyes glancing aside (`pose` in `vary_terms`); the rest face front as the game's portraits do. A description that already says how the head is turned is left alone.
- **The face bank, made to work.** Tested on one description four times, bank faces at 0.77 gave the same man four times: with 4 steps the engine either keeps the photo (0.7) or ignores it (0.75), nothing between. Bank pictures are now painted in 6 steps at 0.68 (the engine runs 6 x 0.68 = 4 steps, so no slower), and four bank faces give four different men. The faces are lined up where a portrait's face sits (eyes a third of the way down, face half the width), split into `faces\human\male` and `faces\human\female` (7,748 faces), and a picture takes a face of its own gender. Dwarves, duergar and gnomes skip the bank (`skip` in `face_bank`): a human face held them to human proportions and took the dwarves' beards. Start from's strength moves from 0.77 to 0.68.
- **Auto: the crop works on a portrait proposed in Propose too.** The Keep and *From Propose* tiles now show the picture the proposal was cut from, with the yellow box; moving the crop (or Set crop) cuts that proposal again to match. Before, the crop only moved on the batch's own pictures.
- **The pool in Auto and Propose.** Right-click a picture on an Auto card > *Add from the pool...* opens the pool chooser (same race and gender first); the spare picked joins that card as one more choice, and leaves the pool once it is proposed. On the Propose tab, up to six spares made for a creature of the same race and gender sit beside the status line: click one to add it to the candidates, click *Pool (n)* for another six, or *More...* for all of them.
- **Apply picks** redraws only the cards it changed, not the whole Auto page (unless Hide picked is on, when they leave the page).
- **Restyle from this.** Right-click any candidate on the Propose tab (one you generated or brought in with Select image) > *Restyle from this* > a strength from 0.50 (keeps the most) to 0.85 (mostly new). Generate then repaints that picture in the creature's description and the chosen style, keeping its pose, hair and clothes, so a picture from another tool can be brought into line with the rest. While restyling, Start from shows *This picture* and the number beside it is the restyle strength, so it can be fine-tuned (0.57, 0.67...) without picking again; choosing another Start from, or right-clicking the picture again, stops it.
- **Face bank and Start from.** Each picture can start from a real face (img2img) instead of from noise, so
  faces differ even when the words are alike. **Start from** (Auto bar and Propose window, one shared setting):
  *Face bank* (default) deals a new face from `faces\<race>` (else `faces\human`) like cards from a shuffled
  deck, so none repeats until all are used; *Its portrait* starts from the creature's own portrait; *Nothing* is
  the old way. The number beside it is the strength (0.77: race, clothes and style come from the words). Beasts,
  monsters, half-orcs, the undead and ghosts never take a bank face; a child only takes one from `faces\child`.
  *Use this face* brings back the start face with the seed. The bank holds 8,000 framed SFHQ faces (picked from
  24,000: no glasses, hats, doll or anime faces, children, closed eyes or turned heads); `face_bank` in
  generator/settings.json sets its folder and default strength.
- **Touch up > Spots: eye color works when you click the middle of an eye.** The click landed on the black pupil, so the color was matched to the pupil (and lashes) and the iris was left alone. When the middle is that dark, the iris color is now read from around the click, and only pixels about as colorful as the iris change, so a warm lid beside an amber eye stays put.
- **Gnomes vary**, the same way: the race line only says a grown-up gnome, and nose, ears, eyes, lines and beard are picked per creature.
- **The sprite's race and gender win** over a creature file's odd ones: Ervin's file says golem and female, but he walks about as a male gnome cleric, so Reset and Auto now make him a male gnome.
- **Goblins, hobgoblins and ogres vary.** Their race lines are cut to what makes the kind (an ogre's tusks, a hobgoblin's goblinoid face, ears and fangs) and the rest is picked per creature in `race_picks`: skin, nose, eyes, ears, hair and beard. Before, eight hobgoblins came out as one face.
- **Halflings vary.** The race line is just "halfling". The two things that mark one are picked per creature in `race_picks`: a broad face (round and rosy, freckled, weathered, plump, strong-jawed) and curly hair (tight curls, loose curls, short, tied back, thinning), plus the nose. Before, every halfling had the same fixed face.
- **Vary faces is always on, and the tick box is gone.** Every picture gets a face of its own. Putting a seed in the Seed box, which is what "Use this face" does, asks for that one face, so no new face words are added. Clear the Seed box (x) to get new faces again. Auto always varies.
- **Everything gets face variety now, not only people:**
  - Beasts (werewolves, kobolds, lizard men, sahuagin, animals): an age, a temper (watchful, wary, a glare, bared teeth), a mark (a torn ear, a scar, a clouded eye) and sometimes coloring.
  - The undead (ghouls, zombies, mummies, liches): a dead stare or lipless grin, and marks of rot (bare bone on the cheek, an empty socket, a cracked jaw).
  - Goblins, orcs and ogres keep their own list.
  - These are the `beast`, `undead` and `monster` lists under `vary_terms`.
- **Animals no longer get a person's face from Vary faces.** A cat got "about sixty years old, a strong aquiline nose, shoulder-length gray hair".
- **Each kobold is its own kobold.** Through `race_picks`, every one gets a hide color, horns, eye color and sometimes a mark (a scar across the snout, a chipped tooth, a notched horn), always the same for the same creature. A dozen Kobold Commoners no longer come out as one face.
- **Kobolds have long lizard snouts:** "a small dog-like reptile creature, not a human: a long narrow lizard snout with the nostrils at its tip, jaws lined with tiny sharp teeth…". "A short snout" came out as a horned man. Vary faces gives them no face shapes, only clothes.
- **Ghouls look like ghouls:** a skull-like head, gray-green skin stretched over the bones, nose slits, a lipless mouth of jagged teeth, small glowing eyes. Ghasts get the same. Vary faces no longer gives the undead (ghouls, ghasts, zombies, mummies, liches, revenants) ages, face shapes and ear rings, which turned them into old men.
- **No beards on women, even from an old saved description.** Propose and Auto take beard and stubble words out of any woman's description before it goes to the engine. A description saved before the looks were fixed still said "thin gray hair and straggly beard" for an old elf woman.
- **Auto: Reroll, One more and Generate no longer redraw the whole page when they finish.** Each card is already drawn in place as its pictures arrive.
- **Auto starts some creatures on Keep.** Analyze now tells the mod's own creatures apart from creatures the game already has that the mod only adds lines to. In the Auto view, a creature that already has a portrait is picked as **Keep** (blue) for you when:
  - it's one the game already has and the mod only adds lines to (Shandalar with more to say), or
  - it's a copy that is still the same character (Face kept).
  - A copy made into someone new (Shandalar copied as "Bob the Wizard") is not, and gets new pictures to pick from as usual.
  - It's only a pick. Click a new picture to change it, or click Keep to take the pick back, which sticks. Needs a fresh Analyze.
- **Reading a mod's own files reads the creatures' colors too:** clothes, hair, skin, metal and drow/duergar sets. They were only read when analyzing the install, so a mod's creatures got descriptions, and Reset results, with no colors.
- **Every Thief sprite wears a hood:** Thief Male, Thief Female, the Halfling and Low versions, and Shadow Thief. It's said with the race as "a deep hood pulled up over the head", and Vary faces doesn't add a second hood.
- **Leather is leather:** the Leather outfit is "a fitted jerkin of supple … leather, laced at the front". "Worn gray leather armor" came out as steel plate.
  - Armor details (pauldrons, gorget) now go only to mail, plate and gambesons.
  - A creature's metal color is said as "buckles and clasps" rather than "fittings".
- **Auto view:** click "N lines" on a card to open that creature's lines, with play, the same window as in the gallery.
- **The Auto view shows 40 creatures a page.** It has the gallery's page bar at the top and bottom: < Previous, a page box (type 12 and press Enter to go to page 12), and Next >. With every card on one page, a batch of 120+ creatures was thousands of widgets: slow to maximize or resize, and cards flashed at the top left while Windows moved them into place.
- **Tooltips no longer pile up on screen.** A tooltip left open when its card was redrawn, as the Auto view does while generating, stayed behind for good. Now a tooltip closes with its widget, and only one shows at a time.
- **Auto view:** click a creature's name to copy it. Right-click it to copy its creature file name.
- **Vary faces for goblins, orcs, ogres and the like.** They had no face variety at all once the human words were taken out. Now they get their own: an age (young, in his prime, grizzled), a face shape, a temper (sullen, sly, glaring, watchful, a crooked grin), a mark (torn ear, broken fang, blind eye) and sometimes a detail (ear ring, nose bone, face paint). These go right after the race's name, so they count. The list is `monster` under `vary_terms`.
- **Right-click a picture in the Auto view** for: Pick this one (or Take the pick back), Open full size, Show in folder, Copy seed, One more picture, Replace with a new one, Delete this picture, and Open in Propose.
- **Auto is a view of the Gallery now, not a tab of its own.** The Gallery opens on the Auto view. An **Auto** tick box beside the Gallery button switches between the two, and the choice is remembered. The wheel and the arrow, Page and Home keys scroll whichever view is showing.
- **Children.**
  - Age has **Child**: a girl or boy of about nine, said first in the prompt, with the style's aging words taken out and no scars or marks.
  - The game's Boy and Girl sprites are children in human form whatever the file's race. Mia the lycanthrope walks around as a human girl, so she's painted as one.
- **Hobgoblins look like hobgoblins:** a burly goblinoid brute with big pointed ears, fangs and a scraggly beard. Vary faces no longer gives goblins, orcs, ogres, kobolds, githyanki and the like a person's eyes, nose and skin tone, which made them men painted red.
- **Women no longer get bald heads or receding hairlines** from the face draw. Rhianna got a bald head under her headscarf and came out looking like a bearded old man.
- **Auto: Set crop.** Locks a creature's crop and shows its pictures cut exactly as the portrait will be. **Edit crop** goes back to the yellow box.
- **Auto keeps going when the engine refuses a picture (500).** It tries once more, then skips that creature. It stops only after three failures in a row.
- **The Drow sprite makes a drow.** A creature whose file says ELF but walks around as "Drow" or "Drow Female" is described as a drow now. The same goes for a duergar on a DWARF file and a svirfneblin on a GNOME file.
- **Auto: Keep no longer hides a later choice.** A creature marked Keep and then proposed in Propose showed nothing as picked, and new picks seemed to vanish. The card now follows the proposal wherever it was made.
- **Keep only shows when there's something to keep.** It used to offer an earlier proposal whose file had since been deleted, which left the creature pointing at a missing picture.
- **Auto only deletes its own .bmp cuts.** It used to delete whatever the card had proposed before, including a picture proposed in Propose.
- **Auto shows proposals made in Propose.** A candidate proposed with Use this turns green on its card. Any other picture proposed there shows on the card as **From Propose**. A proposal removed elsewhere clears the card's mark.
- **Beard picker in Propose**, for men: By race, None, Stubble, Short, Full, Long, Braided.
  - By race gives dwarves, gnomes and duergar their race's own beard, different for each creature, and names no beard for anyone else.
  - None asks for a smooth, clean-shaven chin and upper lip rather than "no beard", which paints one.
- The race beards are plainer: a dwarf gets full, long, bushy or braided. A gnome gets short and wispy, a thin goatee, or none. A duergar gets long, braided or short, always white.
- **`race_picks`** replaces `race_eyes`. It holds things that differ between creatures of one race, and each creature always gets the same one:
  - Drow eye color.
  - Dwarf beards: long, bushy, two braids, forked with rings, braided down the middle.
  - Gnome beards: short and wispy, a goatee, scruffy, or none 1 time in 10.
  - Duergar beards: long white, braided white, forked white.
  - Beards are for men only. They're kept with the race's own words, so Vary faces can't push them back.
- **Duergar look unlike dwarves:** dark charcoal-gray skin with a purple sheen like a drow's, bald, with a white beard. Duergar women have stark white hair pulled back.
- **Gnomes** have a very large pointed nose and big ears, and the men have beards (Quayle does, and Tiax's sprite does).
- **Hair:** dreadlocks are a fairly common choice now, plus hair bound with cords and beads, and beaded tassels for women.
- **Duergar** is in the race list. The line is reworded ("dull ash-gray skin the color of cold stone"), since "ashen gray skin" came out as a pale human. A duergar woman has her own line and no longer gets the men's long gray beard.
- Gnomes checked on Klein, three men and three women: all came out gnomes, with big ears and noses and wrinkled faces.
- Vary faces no longer adds a second hairstyle beside one from the creature's own face draw ("a plain short cut", "a widow's peak").
- **More races in Propose:** Drow, Tiefling, Githyanki, Orc, Goblin, Hobgoblin and Ogre join Human, Elf, Half-Elf, Dwarf, Halfling, Gnome and Half-Orc. A race picked by hand now beats the race hint in the creature's colors, which used to turn a picked Drow back into an elf.
- **Tusks grow from inside the mouth.** Orcs, ogres and half-ogres have tusks described as lower canines rising from the jaw over the upper lip. Said loosely, Klein grew them from the chin. Half-orcs have none.
- **Drow look like drow.** "drow elf, dark gray skin, white hair" came out as a pale gray-haired human on Klein. The race line is now "a dark elf with smooth charcoal-black skin with a faint purple sheen, long pointed ears, stark white hair". Eight tests, men and women, all came out drow.
- **`race_eyes`**: each drow gets red, violet or pale lavender eyes (weighted 60/25/15), always the same one for the same creature.
- A saved description is found again after Analyze regroups a creature's dialogs. Emhrys' drow description had been lost this way.
- **Werewolves came out as men under Vary faces.** It gave them a person's eyes, nose, hair, age and skin tone, and Klein painted a man. Beasts now get no face words, only clothing details. That covers werewolves, fur, muzzles, scales, fish heads, skeletons, golems and the like.
- **Hairstyles weighted.** Men's hair is mostly short and practical now: neat, cropped, untidy, shaggy. Braids come up about 1 time in 100 (they were 1 in 8, and Klein paints any braid as two). Women's styles are weighted too, toward loose, tied back and pinned up. The creature's own face draw no longer has "braided hair". The odds are `hair` and `hair_female` under `vary_terms`.
- **Auto: Crop per creature.** Each picture shows the yellow box of what the portrait keeps. Above Reroll, − and + zoom the box, the arrows move it and Mid resets it. A picture already proposed is cut again when the box changes.
- **Auto: pick now, Apply picks later.** Clicking a picture only picks it (blue). **Apply picks** proposes them all at once. Picks are kept between sessions. Pick first is gone.
- **Male dwarves lost their beards under Vary faces.** The face words were put in the middle of the race's own description, pushing "scraggly beard" back behind a dozen other words. They now go after the whole race description, and six test dwarves all came out bearded.
- **No more snarls or scowls unasked.** Vary faces never picks angry expressions now. Mood has *Angry* and *Fierce* for them.
- **Auto: you can see which picture is picked.** It has a green frame and "✔ Picked A" or "B" under it, and the others say "Pick A" or "Pick B".
- **Auto: Keep.** A creature that had a portrait, or an earlier proposal, shows it first. Click it to keep it: the earlier proposal comes back, or the proposal is removed so the game's own portrait stays.
- **Expressions mostly calm.** Vary faces picks expressions by weight now: about a third calm and neutral, then faint smiles and thoughtful looks, with grins and snarls rare. Mood gets *Any* for that, and *Neutral* now asks for a calm, neutral expression.
- **Auto: Pick first**, on by default, proposes each creature's first picture as soon as it's made. Click the other to switch, or Reroll for new ones.
- **Auto: Redo proposed.** Creatures that already have a proposal are skipped unless this is ticked. Picking a new picture then replaces the proposal.
- **`{random}` in the background is finally replaced** with one of the `background_palettes`, picked fresh for every picture, in both Propose and Auto. Until now it went to the engine as the literal word.
- **Auto tab: a portrait for every creature listed.**
  - One Generate makes 2 candidates per creature, the way Propose would: own description, role, sprite, colors and face, with Vary faces on.
  - The review page shows two creatures to a row. Click a picture to propose it, at the game size.
  - Reroll, Open in Propose and Discard per creature. Discard unchosen and Hide proposed for the whole list.
  - Creatures with a portrait are included unless Skip if has one is ticked. Party NPCs are left out unless ticked.
  - Batches are remembered per mod between sessions.
- **Vary faces no longer adds a second hairstyle** when the description already has the creature's own ("lank shoulder-length hair" plus "long loose hair").
- **A creature known only by name** is described as "person" instead of nothing.
- **Hoods and headscarves are no longer part of an outfit.**
  - The Peasant outfit is just the tunic or kirtle now, and the Sailor role is no longer always in a headscarf.
  - Head coverings come only from sprites that wear them: the Peasant Woman sprite gets a plain headscarf, and the male Thief sprite a hood.
  - Otherwise a hood pushed back or a kerchief turns up now and then as one of Vary faces' clothing details.
- **Vary faces varies more, clothes included.**
  - The face words now go right after the race instead of at the end of the description. Klein reads the start of a prompt hardest, and much of what was said last was being ignored.
  - Each picture also gets brows, and a skin tone for humans whose description doesn't give one. The weights favor fair, tanned, ruddy and olive.
  - Clothes: a color when the description has none (the creature file's color always wins) and one detail: patched sleeves, a shawl, a belt, a cord necklace, a pinned cloak. People in armor get armor details instead: dented pauldrons, a gorget, a fur mantle.
  - The lists are `brows`, `skin`, `cloth_color`, `cloth_detail` and `cloth_detail_armor` under `vary_terms`.
- **Young characters got gray and white hair.** Vary faces read the "old" in "a small faint old scar" as the character's age.
  - It now goes only by the age words.
  - The scar steps say "pale healed scar" instead of "old scar", which also stops Klein painting them as fresh red cuts.
  - A young face no longer draws a weathered, gaunt or jowly face shape.
- **Vary faces leaves the creature file's hair color alone.**
  - It used to miss "sandy hair" and "hair dyed ...", and then added a second, random color.
  - "hair dyed green" is no longer garbled into "close-cropped hair dyed green hair".
- **Game folder and Mod folder share one row**, each with its own Browse button and ⤢ button. The ⤢ box with OK and Cancel is still there for long paths.
- **Vary faces weights hair colors.**
  - Odds for most faces: brown 40, black 30, dark brown 15, blond 8, auburn 4, red 3.
  - Gray, graying, salt-and-pepper and white come only with age, when the face is fifty or older or the description says old.
  - Before this, all seven colors were equally likely, so about one face in three got red or auburn hair.
  - The odds are `hair_color` and `old_hair_color` under `vary_terms`.
- **Scars and Grime pickers in Propose.**
  - Scars has five steps: None, Few, Scarred, Heavy, Wounded. Few is the default, also for creatures generated from their own file.
  - Grime has four steps: Clean, Smudged, Dirty, Filthy.
  - Klein painterly no longer says "subtle facial scarring". That gave everyone the scarring that Scarred now gives.
  - Scarred and Grimy left Look.
  - Each step was checked on Klein, and it adds a little more than the step before.
- **Roles cut down to the games' own sprites:** Fighter, Cleric, Mage, Thief, Monk, Soldier, Noble, Peasant, Beggar, Sailor, Courtesan.
  - Look, Mood and Outfit are trimmed to match. Beggar covers Poor and Slave, Sailor covers Pirate, and Soldier covers Guard.
  - The first role word in an animation name wins. A Harlot Fighter is drawn as a courtesan and a Nobleman Fighter as a noble.
- **`sprite_terms`** replaces `animation_terms`, `front_animations` and `bald_animations`. It holds what a sprite itself shows:
  - The Fat Man is very fat, bald and clean-shaven. The Fat Woman is fat but keeps her hair; she used to be drawn bald.
  - The Ogre, Half Ogre and Hobgoblin wear their horned helmets, and these sprites get no clothes added.
  - The helmets came out of the race lines, so an Ogre Mage no longer wears one.
- **Fixes:**
  - Shadow Thieves were drawn as living shadows. They're people in black now.
  - The Fat Man could get a hair color from his creature file.
- **Propose pickers redone so each says one thing.** The old lists mixed things up: Class held both classes and stations, Look held expressions and "Noble", and Cloth named a material while the class named a garment.
  - Now there are Role, Age, Look (the face), Mood (the expression) and Outfit (what they wear).
  - A role brings its own outfit unless Outfit overrides it: Fighter gets padded armor, Paladin plate, Farmer a homespun tunic and hood, Noble a velvet gown or doublet. The creature's colors are worked into the outfit.
  - Every role was checked on Klein, and all came out medieval.
  - The settings move into `role_terms`, `outfit_terms` and `mood_terms`. `cloth_terms`, `clothes_worn` and `clothes_fine` are gone, and `animation_terms` keeps only the body words (fat, boy, girl).
- **Klein painterly** opens with "A character from a medieval fantasy world, in clothes of the Middle Ages." Farmers,
  laborers, servants and sailors came out in button-up work shirts and striped sailor tops; with it, laced tunics.
- **Young** looks young: "A young woman of about twenty, with youthful smooth skin." goes first in the prompt
  and the style's "weathered appearance" and "facial wrinkles" are left out for her (`young_lead`, `young_drop`
  in generator/settings.json). Vary faces gives a young character no gray or white hair, and women draw their
  hair from `hair_female` (braids, plaits, long hair) instead of crops and shaved sides.
- A creature's drawn face words (face_detail) leave out what its race already says: a halfling no longer gets
  "a fine-boned face, a bulbous nose, close-cropped hair" on top of its round face, button nose and curls.
- **Klein painterly** has no LoRA. The painterly LoRA pinned every picture to one woman whatever the seed;
  without it Klein gives different people from seed to seed and keeps the painted BG look from the wording.
- **Candidates** go up to 100 per Generate, set by `max_candidates` in generator/settings.json (was 50, fixed).
- **Forge Neo with FLUX.2 Klein 9B** is the engine (generator/start_engine_neo.bat), with the **Klein painterly**
  style. Every race was run through it: Klein follows the words closely, so the halflings' "double chin, jowls"
  (needed on Flux.1) made plain fat people - they are now small, plump and cheerful with apple cheeks and curls.
  face_detail is "full": Klein gives near-identical faces from seed to seed, and the per-creature face words
  (shape, nose, hair) are what tell one commoner from the next.
- **Missing picture(s)** counts only creatures that are listed. A picture proposed for a whole bunch before it
  was taken apart one by one (and hidden party NPCs or ignored creatures) was counted, so the button said 2
  missing while ticking Missing showed nothing.
- **BG2 framing** uses the portrait LoRA at 0.35 instead of 0.65: the clothes asked for come through (rags,
  robes) instead of the armor and leather coat the LoRA was trained on. Pick a higher strength beside the
  LoRA box for an adventurer.
- **Reset** sits on the Built from row with the pickers it resets.
- **Propose tab:** In-game, Proposed and where the description came from sit on the creature's name row;
  the race, gender, class, age and look pickers have a row of their own under it ("Built from:").
- A woman no longer gets the beard or stubble written into some looks (Gaunt and old, Weathered...).
- A noble's clothes are fine wool rather than faded and worn, by the animation as well as the Noble look.
- **Speckle it** keeps every speckle inside the ring that was clicked; they used to scatter past it.
- **Paint these in** no longer darkens the speckles: it left the speckle amount set and cleared the marks, so
  a second, unplaced set was laid over the face on top of the painted-in one.
- **Githyanki** open with what they are - "a gaunt alien warrior with jaundiced, leathery yellow skin" - then a
  hard, hollow-cheeked face, slitted nose, swept-back ears and the topknot. Before, they came out as humans with
  elf ears and only now and then yellow.
- The **Animal (no person)** style no longer carries over to people. Animals and people each remember the
  style last used for them: open an ogre after a cat and it is back on your people style (BG2 framing until
  one is used), and opening a cat or a dog picks the animal style.
- **Ogres** are described as huge, ugly, warty giant men with a human-like head, a big bulbous nose, an
  underbite with tusks and sallow skin. "Huge lumpen head, yellow-brown skin, heavy jaw" came out as an ape.
- **Fat Man** sprite: the description now opens "very fat, bald, clean-shaven male human: a huge, round,
  fleshy face...". "Heavyset, round face" after the clothes came out as an ordinary man with a beard; even
  saying "no beard" paints one, so beards are not named at all. New setting `front_animations` says which
  animation words lead the description like this.
- **Touch up: scars, wounds and grime redone.** A scar follows a slightly bowed, wandering line that thins to
  nothing at both ends, paler than the skin with a dark edge and uneven, puckered paint, and takes its color
  from the skin around it rather than whatever brow or lash it was clicked on. A wound is a ragged cut with raw
  lips, reddened swelling along it and drips that run down, not a straight red line on a purple disc. Grime
  lies on the skin only - no longer on clothes and background - always darker than what it covers, in
  smudges with a little grit.
- **Scarred** and the new **Grimy** look go in right after the race instead of at the end of the
  description, where Flux mostly ignored them.
- **Main screen tidied.** *Read Mod Files* is gone: Analyze reads the mod's own files by itself when the mod
  is not installed in the game folder. *Merge same names* is gone (the command line keeps `--merge-names`).
  *Save as text* and *Save as CSV* are one **Save list...** button on the Proposals tab. The filters - Party
  NPCs, Ignored, the Show only boxes and Mod's portraits only - are one row above Gallery, Table and
  Proposals, since they apply to all three; before, they sat on the Gallery tab and still filtered the Table.
- The **expand** boxes for Description and Background show their OK button again (the text box pushed it off
  the bottom), take Ctrl+Enter, and save what you typed straight away instead of only on Generate.
- **Elves, half-elves, dwarves, gnomes and tieflings** say what makes them one. A bare "gnome" was painted as a
  bearded man and a tiefling with one horn; a tiefling now gets two horns and keeps the creature's own skin.
- **race_terms_female** in the generator settings: words used instead of race_terms for a woman of that race.
  Dwarf and gnome women came out as slim pretty humans with braids; they are now said to be in their fifties
  and homely rather than pretty, with a dwarf's broad heavy face or a gnome's long nose.
- **Halflings** are described as grown-ups with a very broad, chubby round face, heavy cheeks and jowls and
  thick curls. A bare "halfling" was painted as an ordinary young man, and a round face alone as a child.
- A creature's clothing color goes into the description as faded wool, worn and weathered (fine wool for the
  Noble look). "Dressed in orange with red trim" was painted as a modern nylon jacket or hoodie.
- **BG2 framing** and **Painterly** styles open with "a character from a medieval fantasy world, in clothes of
  the Middle Ages". Flux ignores the negative prompt that said "modern clothes", and a bright color such as
  orange came out as a modern jacket or hoodie; said first in the prompt, the period holds.
- **Animal (no person)** paints the animal itself at full portrait size. In the tall frame the oil-painting
  LoRA and its trigger word filled the body with a character's clothes, giving a cat-headed person; the style
  now leaves the trigger word out, keeps the LoRA light and asks for a real animal's own body.
- **Faster analysis** of a big install: matching a dialog to a creature by name no longer goes through every
  creature file each time, and the game's BIF files stay open while the analysis runs instead of being
  opened again for every file read from them.
- **Animal (no person)** style, for a portrait of an actual animal. The other looks carry a
  portrait LoRA trained on humanoid faces and ask for a character portrait with "shoulders and collar
  fully clothed", so a cat came out as an anthropomorphic cat in clothes however the description was
  worded. This one keeps only the oil-painting LoRA, which is a painting style and not a face, asks for
  the animal as it is, and drops the shared negative prompt, which argues against bare shoulders.
- **Race and class can be typed** in the picture maker, not only picked from the list - "cat", "owl",
  "wolf". A race the settings do not know is used as it is written.
- **Right-click a picture in the pool chooser** to delete it, as in the Pool tab.
- **Tick page** and **Untick all** on the pool toolbar, in place of the one button called "None".
  Tick page fills the page, or empties it when it is already full, and leaves the other pages alone;
  Untick all clears every page. Both are in the right-click menu too.
- **Candidates now goes up to 50**, not 8. The box let you type a larger number but
  Generate quietly clamped it to eight.
- **Web UI** button beside Start and Stop, which opens the generator's own page in your browser. It is
  grayed out while the engine is not running. The same thing is still under the Engine menu.
- **Generate never paints the same face twice.** A creature's seed used to be worked out
  from its creature file, so the face was the same person every time - which meant closing the Propose
  tab and generating again handed back the four faces you already had, and a spare sitting in the pool
  could come back as a "new" candidate. The engine now rolls its own seed every time. **New face**
  still re-draws the face in words (shape, nose, hair) and keeps them.
- The pool reads its folder again each time the tab is opened, so pictures made since
  show up, and lays out as many per row as the window is wide instead of always four, at the width a
  gallery card is given, so the last one in the row is not cut off at the edge. The wheel
  scrolls it from anywhere over the cards, and over the pictures in the pool chooser.
- Esc closes a window. Seventeen of them had no way out but the mouse.
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
