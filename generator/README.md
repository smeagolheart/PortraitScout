# Portrait generator (optional)

Lets Portrait Scout make candidate portraits for a creature: it turns the creature's gender, race and animation
type into a description, adds your extra details, and asks an image generator for a few candidates.

**No image generator and no models come with Portrait Scout.** They are many gigabytes and separately licensed,
so they are not in this repository and never will be. This folder is only the part that talks to one: it builds
the prompt, sends it, and turns the picture into a game portrait. You supply the generator - either a private
copy of Forge Neo or Forge as described below, or one you already run.

Everything here is optional. Without it Portrait Scout works as before; the **Propose** tab just explains what
is missing.

## Setting up the engine (once)

There are two ways, and Portrait Scout talks to either. **Forge Neo** is what the newest styles are written
against; the older **Forge** package is less to download and needs no git.

### Forge Neo with FLUX.2 Klein 9B (what Portrait Scout is set up for)

[Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo) is a maintained fork of Forge that
paints FLUX.2 Klein, which the older Forge cannot. Klein is step-distilled: **4 steps at CFG 1**, about two to
three seconds a picture on a fast card. Windows, an NVIDIA card, roughly 18.4 GB of model files and 16 GB of
graphics memory at fp8.

1. Install [git](https://git-scm.com/downloads) and [uv](https://github.com/astral-sh/uv#installation), then
   open a command window in the Portrait Scout folder.
2. Clone Forge Neo into `engine-neo` (**Run it** in the setup window types this into a command window for you):

   ```
   git clone https://github.com/Haoming02/sd-webui-forge-classic engine-neo --branch neo
   ```

3. Make its Python, from inside `engine-neo` (`start_engine_neo.bat` starts it with `--uv`):

   ```
   uv venv venv --python 3.13 --seed
   ```

4. Put the three model files in place (create the folders if they are not there):

   | File | Copy to | From |
   |---|---|---|
   | `flux-2-klein-9b-fp8.safetensors` (9.4 GB) | `engine-neo\models\Stable-diffusion\` | [black-forest-labs/FLUX.2-klein-9b-fp8](https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8/resolve/main/flux-2-klein-9b-fp8.safetensors?download=true) (sign in and accept the license first) |
   | `qwen_3_8b_fp8mixed.safetensors` (8.7 GB) | `engine-neo\models\text_encoder\` | [Comfy-Org/vae-text-encorder-for-flux-klein-9b](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/resolve/main/split_files/text_encoders/qwen_3_8b_fp8mixed.safetensors?download=true) |
   | `flux2-vae.safetensors` (336 MB) | `engine-neo\models\VAE\` | [the same repository](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/resolve/main/split_files/vae/flux2-vae.safetensors?download=true) |

   Klein reads the text encoder and the VAE as separate files, unlike the old all-in-one nf4 checkpoint. The
   undistilled [FLUX.2-klein-base-9B](https://huggingface.co/black-forest-labs/FLUX.2-klein-base-9B) works too,
   at about 50 steps and CFG 4 - slower, and not what the styles here are tuned for. FLUX.2 Klein is under
   Black Forest Labs' non-commercial license; read it before selling anything painted with it.

5. Point `settings.json` at it. **Use Forge Neo** in Portrait Scout's setup window (Setup, top right)
   does this for you, with your own paths; **Check files** there ticks each step that is done. By hand:

   ```json
   "engine": { "start": "generator\\start_engine_neo.bat {port}" },
   "payload": {
     "steps": 4, "cfg_scale": 1, "sampler_name": "Euler", "scheduler": "Simple",
     "width": 656, "height": 1024,
     "override_settings": {
       "sd_model_checkpoint": "flux-2-klein-9b-fp8.safetensors",
       "forge_additional_modules": [
         "D:\\PortraitScout\\engine-neo\\models\\text_encoder\\qwen_3_8b_fp8mixed.safetensors",
         "D:\\PortraitScout\\engine-neo\\models\\VAE\\flux2-vae.safetensors"
       ]
     }
   }
   ```

   `forge_additional_modules` takes **full paths**, so put your own drive and folder in. Everything else in
   `payload` is sent untouched.

`start_engine_neo.bat` also passes `--forge-ref-a1111-home ..\engine\webui`, so if the older Forge is still
there its models, LoRAs, VAE and text encoders are read as well and nothing has to be moved or copied. Extra
Forge options can go in the `PS_ENGINE_ARGS` environment variable.

The **Klein painterly** style is the one written for this model, and it carries no LoRA on purpose: the
painterly LoRA had a face baked into it, so every seed painted the same person. Klein also paints nearly the
same face for the same words whatever the seed, which is what **Vary faces** and the face bank are for.

### The older Forge with FLUX.1 dev (one package, one model file)

A private copy of [Stable Diffusion WebUI Forge](https://github.com/lllyasviel/stable-diffusion-webui-forge) in
an `engine` folder next to this one. Windows and an NVIDIA card; the model wants roughly 8 GB of graphics
memory or more, and about 15 GB of disk with the engine.

1. Download [`webui_forge_cu121_torch231.7z`](https://github.com/lllyasviel/stable-diffusion-webui-forge/releases/download/latest/webui_forge_cu121_torch231.7z) (CUDA 12.1 + Torch 2.3.1, 1.7 GB; other builds are on the [releases page](https://github.com/lllyasviel/stable-diffusion-webui-forge/releases)) and
   unpack it into `PortraitScout\engine`, so that you get `PortraitScout\engine\webui\` and
   `PortraitScout\engine\system\`, with `run.bat`, `update.bat` and `environment.bat` directly in `engine`.
2. Run `engine\update.bat` once.
3. Run `engine\run.bat` once and wait until the Forge page opens in your browser (the first run installs the
   rest and takes a while). That shows it works. Close the page and the black window again.
4. Put the model files in place (create the folders if they are not there):

   | File | Copy to | From |
   |---|---|---|
   | `flux1-dev-bnb-nf4-v2.safetensors` (12 GB) | `engine\webui\models\Stable-diffusion\` | [lllyasviel/flux1-dev-bnb-nf4](https://huggingface.co/lllyasviel/flux1-dev-bnb-nf4) on Hugging Face |
   | `chargen_oil_painting_v2.safetensors` (86 MB) | `engine\webui\models\Lora\` | [Painterly Fantasy Portrait](https://civitai.com/models/850634) on Civitai - **the "BG2 oil painting" style asks for this one** |
   | `BG_Portrait.safetensors` (38 MB) | `engine\webui\models\Lora\` | [Baldur's Gate 2 Portrait Style Flux](https://civitai.com/models/2071031/baldurs-gate-2-portrait-style-flux) on Civitai - used by the "BG2 framing, light LoRA" style |

   Other models work too: name the checkpoint in `payload.override_settings` and the LoRA in the style prompts
   of `settings.json`. FLUX.1 wants 20-30 steps, CFG 1 and distilled CFG about 1.6; SDXL and SD1.5 want CFG 5
   to 7 and no distilled CFG.

### Either way

Portrait Scout runs the engine by itself from then on: the `start` command in `settings.json` starts it without
a window and without opening its page, the first time you press **Generate** (or **Start**). The first start
loads the model and can take a few minutes; progress is shown in the window. It is stopped when you close
Portrait Scout, unless **Leave running when Portrait Scout closes** is ticked; then use **Stop** when you want
the graphics memory back. Do not run two engines at once: both would want the graphics card's memory.

To go back and forth, `settings.json` keeps the other one beside it - `_start_old` is the older Forge's start
command and `_payload_old` the settings that go with it. Swap them into `start` and `payload`.

The same steps, with buttons that open each download and folder, are on the **Propose** tab when no engine is
found, and under **Engine > Setup...**

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
- `start_engine.bat` - starts the older Forge (`engine\`) with its API on, without opening its page.
- `start_engine_neo.bat` - the same for Forge Neo (`engine-neo\`), which also reads the old engine's models.
- `settings.json` - your settings. `engine.log` - what the engine printed the last time it was started.
- Candidates are saved in `PortraitScout\generated\<mod>\`.
