# Portrait generator (optional)

Lets Portrait Scout make candidate portraits for a creature: it turns the creature's gender, race and animation
type into a description, adds your extra details, and asks an image generator for a few candidates.

**No image generator and no models come with Portrait Scout.** They are many gigabytes and separately licensed,
so they are not in this repository and never will be. This folder is only the part that talks to one: it builds
the prompt, sends it, and turns the picture into a game portrait. You supply the generator - either the private
copy of Forge described below, or one you already run.

Everything here is optional. Without it Portrait Scout works as before; the **Propose** tab just explains what
is missing.

## Setting up the engine (once)

The engine is a private copy of [Stable Diffusion WebUI Forge](https://github.com/lllyasviel/stable-diffusion-webui-forge)
in an `engine` folder next to this one. Neither it nor the models are part of the Portrait Scout download (about
15 GB together). You need Windows and an NVIDIA graphics card; the default model wants roughly 8 GB of graphics
memory or more.

1. Download [`webui_forge_cu121_torch231.7z`](https://github.com/lllyasviel/stable-diffusion-webui-forge/releases/download/latest/webui_forge_cu121_torch231.7z) (CUDA 12.1 + Torch 2.3.1, 1.7 GB; other builds are on the [releases page](https://github.com/lllyasviel/stable-diffusion-webui-forge/releases)) and
   unpack it into `PortraitScout\engine`, so that you get `PortraitScout\engine\webui\` and
   `PortraitScout\engine\system\`, with `run.bat`, `update.bat` and `environment.bat` directly in `engine`.
2. Run `engine\update.bat` once.
3. Run `engine\run.bat` once and wait until the Forge page opens in your browser (the first run installs the
   rest and takes a while). That shows it works. Close the page and the black window again.
4. Put the two model files in place (create the folders if they are not there):

   | File | Copy to | From |
   |---|---|---|
   | `flux1-dev-bnb-nf4-v2.safetensors` (12 GB) | `engine\webui\models\Stable-diffusion\` | [lllyasviel/flux1-dev-bnb-nf4](https://huggingface.co/lllyasviel/flux1-dev-bnb-nf4) on Hugging Face |
   | `chargen_oil_painting_v2.safetensors` (86 MB) | `engine\webui\models\Lora\` | [Painterly Fantasy Portrait](https://civitai.com/models/850634) on Civitai - **the style Portrait Scout starts with asks for this one** |
   | `BG_Portrait.safetensors` (38 MB) | `engine\webui\models\Lora\` | [Baldur's Gate 2 Portrait Style Flux](https://civitai.com/models/2071031/baldurs-gate-2-portrait-style-flux) on Civitai - used by the "BG2 framing, light LoRA" style |

   Other models work too: name the checkpoint in `payload.override_settings` and the LoRA in the style prompts
   of `settings.json`.

After that Portrait Scout runs the engine by itself: `start_engine.bat` starts it without a window and without
opening the Forge page, the first time you press **Generate** (or **Start**). The first start loads the model and
can take a few minutes; progress is shown in the window. It is stopped when you close Portrait Scout, unless
**Leave running when Portrait Scout closes** is ticked; then use **Stop** when you want the graphics memory back.
Do not run another Forge at the same time: both would want the graphics card's memory.

## Changing the look

Everything is in `settings.json` (written on first use):

- `styles` - the looks offered under **Style**, each a prompt (and optionally its own negative prompt). `{subject}`
  is the short description ("female elf"), `{character}` the full one plus your extra details. `style` names the
  default. `prompt` / `negative_prompt` are used when there are no styles.
- `payload` - sampler, steps, picture size, model: sent to the engine as is.
- `portrait_bmp` - the game portrait that **Use this** makes: `width` and `height` (169 x 266, as the HPS
  portraits), `zoom` (how close the crop is; 1 = the whole picture) and `top` (where the crop starts, 0 = the
  very top). Size and zoom can also be changed in the window, which remembers them.
- `race_terms`, `animation_terms` - how game data is worded ("half orc" -> "half-orc, pale green skin, muscular").
- `candidates` - how many pictures per press (you can change it in the window too).

## Using your own generator

Anything that answers the Automatic1111 / Forge API (`POST /sdapi/v1/txt2img`) will do - a Forge or
Automatic1111 install you already have, SD.Next, a copy on another machine on your network. In `settings.json`:

```json
"engine": {
  "url": "http://127.0.0.1:7860",
  "autostart": false
}
```

- `autostart: false` means Portrait Scout never starts or stops it; you run it yourself with `--api` on.
- To have Portrait Scout start yours instead, leave `autostart: true` and put your own command in `start`
  (`{port}` is taken from `url`). It is run from the Portrait Scout folder. **Stop** in the window only works if
  the generator was started with `--api-server-stop`.
- `payload` is sent to the generator untouched, so the model, sampler, steps, size and anything else your
  generator accepts go there - including `override_settings` for the checkpoint.
- The prompts are yours: edit them in the **Prompt** box on the **Propose** tab (**Save**, **Save as new style...**,
  **Revert to baseline**) or in `styles` here. A style is just a name, a prompt, an optional negative prompt and
  an optional `zoom`.
- Nothing else in Portrait Scout needs changing, and `portrait_generator.py` itself only needs editing if your
  generator speaks some other API.

## Files

- `portrait_generator.py` - description, prompt and the engine client (standard library only).
- `start_engine.bat` - starts the engine with its API on, without opening the Forge page.
- `settings.json` - your settings. `engine.log` - what the engine printed the last time it was started.
- Candidates are saved in `PortraitScout\generated\<mod>\`.
