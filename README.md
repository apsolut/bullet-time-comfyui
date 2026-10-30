# Bullet Time from one phone shot

**Freeze any moment of your take and fly the camera around it. Then the take carries on.**

Project page: https://apsolut.github.io/bullet-time-comfyui/

You film one normal clip on your phone: a jump, a hair flip, a throw, a splash. Bullet Time finds the peak of
the action, freezes it, and generates the camera move that a ring of 100 cameras would have given you:
an orbit to the side, a 180° Matrix sweep, a half orbit behind you, or a crane up. The camera then flies back
to exactly your real frame, and your real take continues with its real sound.

A Higgsfield-style effect, rebuilt as an open app for the Comfy *Open Source Higgsfield* challenge
(#OpenSourceHiggsfield). **Open weights end to end**: Qwen-Image-Edit 2511 (Multiple-Angles LoRA) makes the
views, **LTX-2.3** is the core video model that moves the camera through frozen time. No partner or
closed API nodes. MIT licensed. A sibling of [one-shot-multicam](https://github.com/apsolut/one-shot-multicam).

```
real take ..... | slow-mo ramp | camera orbits the frozen moment | rewinds home | real take resumes .....
real audio .... | pitched down | generated whoosh (LTX audio)     | reversed     | real audio ............
```

## How it works

1. **Pick the moment (free, CPU).** OpenCV scores every frame by *motion x sharpness*: bullet time wants the
   peak of the action, but the peak is where frames blur, and a blurred freeze becomes a blurred statue. The
   sharpest frame near a motion peak wins. Frames with closed eyes are ruled out first with
   [MediaPipe Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker) (open model,
   Apache-2.0, 3.7 MB, downloaded once): a blink in the frozen frame is copied into all 8 views and the face drifts.
   You can scrub and pick any frame yourself; a closed-eyes pick is moved to the nearest open-eyes frame.
2. **8 views of the frozen frame (1 Qwen job, ~$0.045).** The Comfy Cloud template *Qwen 1 Click Multiple
   Character Angles* with all 8 prompts in the LoRA's trained format: `<sks> front-right quarter view
   eye-level shot medium shot`, right side, back-right, back, front-left, left side, back-left and a high
   angle. (Free text like "Rotate the camera 45 degrees" comes back rolled 90° on vertical frames; the trained
   format doesn't.) Each still is checked: wrong orientation or a face lying sideways is rejected and that
   keyframe is skipped.
3. **The orbit (1 LTX-2.3 job, ~$0.055).** Your **real frame** is placed in the latent at its frame index and
   each view becomes an `LTXVAddGuide` keyframe (frames 0, 24, 48… so the camera passes through every view).
   LTX-2.3 animates the camera between them while the prompt holds time still ("suspended mid-motion like a
   statue… only the camera moves"). It generates the whoosh in the same pass. Two stages: 352x640, then a x2
   latent upscale with the same keyframes at full resolution.
4. **Splice (free, ffmpeg).** A 0.4 s slow-mo ramp into the freeze (audio slowed and pitched down), the orbit,
   the rewind home at 2x or 2.5x, then your take from the very frame it froze on. The take is never retimed
   outside the ramp, so speech before and after stays in sync with its own audio. Every arc starts and ends on
   the real frame, so both cuts land on the same image.

| Arc | Keyframes | Freeze |
|---|---|---|
| `orbit-right` / `orbit-left` | real → front quarter → side | 4.0 s |
| `sweep` (Matrix) | left side → front-left → **real** → front-right → right side, played real → right → left → real | 4.8 s |
| `half-right` | real → front-right → right → back-right → back | 5.6 s |
| `orbit-360` | real → all 7 views around → real | 6.4 s |
| `crane` | real → high angle | 3.0 s |

About **$0.10 per bullet time** on Comfy Cloud. Re-rolling only the orbit (same views) is one job; changing
the ramp and re-rendering is free.

## Quick start

Needs Python 3.10+ and `ffmpeg` on PATH. No local GPU: generation runs on Comfy Cloud with your key.

```bash
git clone https://github.com/apsolut/bullet-time-comfyui && cd bullet-time-comfyui
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
bullettime serve                                  # → http://127.0.0.1:8790
```

Paste your [Comfy Cloud key](https://platform.comfy.org/profile/api-keys), drop a clip, check the moment,
pick an arc, **Generate**. The key stays in your browser's localStorage and is forwarded per request to your
local server, which forwards it to `cloud.comfy.org` (Comfy Cloud's API has no CORS, so a page can't call it
directly). It is never written to disk or logged.

CLI:

```bash
export COMFY_API_KEY=...                          # never commit this
bullettime analyze take.mp4                       # free: where would it freeze?
bullettime -P work run take.mp4 --arc sweep --dry-run      # plan + cost, spends nothing
bullettime -P work run take.mp4 --arc sweep --out bt.mp4   # everything
bullettime -P work orbit --arc orbit-left         # re-roll with the same views (1 job)
bullettime -P work orbit --guide 0.8              # stick closer to the views (default 0.6)
bullettime -P work render --ramp 0.8 --slow 4     # free re-render
bullettime workflow --arc sweep > sweep_api.json  # the exact graph, for ComfyUI
```

## Workflows

`workflows/` holds the graphs in API format (what `POST /prompt` takes), each with a `*_bindings.json` that
maps the app's inputs to node ids. The orbit graph is built per job by `bullettime/workflows.py` (the number
of keyframes depends on the arc and on which views passed the check); two examples are committed. In the web
app, **ComfyUI workflows** downloads the exact graphs submitted for your result.

| File | What it does | Models |
|---|---|---|
| `orbit_stills_api.json` | 8 views of the frozen frame in one job | [Qwen-Image-Edit-2511](https://huggingface.co/Qwen/Qwen-Image-Edit-2511) + [fal Multiple-Angles LoRA](https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA) + Lightning 4-step (Apache-2.0) |
| `bullettime_orbit-right_api.json`, `bullettime_sweep_api.json` | Keyframed camera move through frozen time, with audio | [LTX-2.3](https://huggingface.co/Lightricks/LTX-2.3) 22B dev fp8 + distilled LoRA 0.6 + x2 spatial upscaler, Gemma 3 text encoder (LTX-2 Community License, Gemma Terms) |

## Tests

`pytest` runs the whole pipeline offline (Comfy Cloud faked, real ffmpeg and OpenCV): graph integrity,
LoRA prompt format, the moment picker, durations, cut continuity, no-audio takes and the web API.

## Limitations

- Qwen invents what the phone never saw (the back of your head, the far side of the room). Back views are the
  weakest; `orbit-*` and `sweep` stay in front of you.
- LTX can let a frozen person drift slightly (a blink, hair moving). Re-roll the orbit, or name the person in
  *Who is frozen?* so the prompt is specific.
- One person, one take, up to 30 s. Output is upscaled from 704x1280.

## Ethics

Only use your own face, or the face of someone who agreed. Don't post it where it could pass as real footage
of an event.

## License

Code: MIT © 2026 Aleksandar Perisic. Models keep their own licenses (see above). The Comfy Cloud client and
orientation checks come from [one-shot-multicam](https://github.com/apsolut/one-shot-multicam) (MIT, same author).
