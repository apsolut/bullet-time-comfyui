"""The two ComfyUI graphs Bullet Time submits (API format, open weights only).

1. Orbit stills: the Comfy Cloud "Qwen 1 Click Multiple Character Angles" template (Qwen-Image-Edit-2511 +
   fal Multiple-Angles LoRA) with its 8 branch prompts exposed (workflows/orbit_stills_api.json). One job
   turns the frozen frame into 8 views around the person.
2. Orbit: LTX-2.5 22B distilled (default, one stage) or LTX-2.3 22B (distilled LoRA, two stages), built here. The real frozen frame is put in place at
   frame 0, every other view is an LTXVAddGuide keyframe at its frame index, and LTX animates the camera
   between them while the prompt keeps time frozen. Audio is generated in the same pass (the whoosh).

Regenerate the shipped example graph: python -m bullettime.workflows > workflows/bullettime_orbit_api.json
"""
from __future__ import annotations

import json
from pathlib import Path

from . import arcs, comfy

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / "workflows"

CKPT = "ltx-2.3-22b-dev-fp8.safetensors"
DIST = "ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors"
TEXT_ENCODER = "gemma_3_12B_it_fp4_mixed.safetensors"
UPSCALER = "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"
ID_LORA = "ltx-2.3-id-lora-talkvid-3k.safetensors"  # LTX-2.3 identity LoRA (open weights), optional
# LTX-2.5 (optional engine): transformer-only distilled model, its own video/audio VAEs and a Gemma 4 text encoder
# with the projection baked in, single stage, no checkpoint (from Comfy's video_ltx2_5_flf2v template).
LTX25 = {"unet": "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
         "vae": "ltx-2.5-video-vae-bf16.safetensors", "audio_vae": "ltx-2.5-audio-vae-bf16.safetensors",
         "clip": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
         "upscaler": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"}
# first entry is the default (the web dropdown pre-selects it): LTX-2.5 since 2026-10-30, 2.3 stays one switch away
ENGINES = ("ltx25", "ltx23")
DEFAULT_ENGINE = ENGINES[0]
ENGINE_LABELS = {"ltx25": "LTX-2.5 (default)", "ltx23": "LTX-2.3"}
SIG8 = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
SIG3 = "0.909375, 0.725, 0.421875, 0.0"
NEGATIVE = ("people moving, walking, talking, lips moving, blinking, gesturing, hands moving, head turning, "
            "person turning around, zoom out, morphing, warping, melting face, extra limbs, "
            "camera shake, cut, scene change, cartoon, pc game, video game, ugly, deformed face, extra fingers")
FPS = 24
# Strength of the generated views as keyframes (the real frame is always 1.0). At 1.0 LTX hard-cuts to a view
# whose background doesn't match the previous one; lower lets it bend the view into a continuous move.
GUIDE_STRENGTH = 0.6


def orbit_prompt(arc: str, subject: str = "", sound: str = "whoosh") -> str:
    who = (subject or "the person").strip().rstrip(".")
    sounds = {
        "whoosh": "Sound: a deep, slowed-down cinematic whoosh with a low sub-bass rumble, like time stretching. "
                  "No voices, no music.",
        "silent": "Sound: silence.",
    }[sound]
    return (f"Bullet time shot. Time is completely frozen: {who} is perfectly still, suspended mid-motion like a "
            "statue, not blinking, not breathing, hair and clothes frozen in place, dust and droplets hang "
            "motionless in the air, mouth and hands locked exactly as they are. The person does not turn; only the camera moves: a smooth, continuous, steady "
            f"{arcs.ARCS[arc]['move']} {who}, at the same distance, keeping {who} centered in frame while the "
            "background slides past with strong parallax. Cinematic, photorealistic, sharp focus, 35mm film. "
            + sounds)


def build_orbit(arc: str, width: int = 704, height: int = 1280, dist_strength: float = 0.6,
                guide_strength: float = GUIDE_STRENGTH, keys: list[str] | None = None,
                color_match: float = 0.0, id_lora: float = 0.0, engine: str = DEFAULT_ENGINE) -> tuple[dict, dict]:
    """-> (API workflow, bindings). keys: the arc's views that have a usable still (default: all of them).
    The real frame is LoadImage "real.png"; view v is LoadImage "kf_<v>.png".
    color_match > 0: every decoded frame is colour-matched (KJNodes ColorMatch, mkl) to the real frame at that
    strength, so the orbit's grade doesn't drift away from the take it is spliced into."""
    a = arcs.ARCS[arc]
    keys = [k for k in a["keys"] if keys is None or k in keys or arcs.is_real(k)]
    idx = dict(zip(a["keys"], arcs.key_frame_idx(arc)))
    length = arcs.frames(arc)
    W: dict = {}

    def n(i, cls, **inp):
        W[str(i)] = {"class_type": cls, "inputs": inp}
        return str(i)

    if engine == "ltx25":
        # the checkpoint's roles split into four loaders; `ck` keeps slot 0 = model, 2 = video VAE for the rest
        unet = n(1, "UNETLoader", unet_name=LTX25["unet"], weight_dtype="default")
        vae = n(9, "VAELoader", vae_name=LTX25["vae"])
        avae = n(2, "VAELoader", vae_name=LTX25["audio_vae"])
        te = n(3, "CLIPLoader", clip_name=LTX25["clip"], type="ltxv", device="default")
        model = unet
        VAE = [vae, 0]
    else:
        ck = n(1, "CheckpointLoaderSimple", ckpt_name=CKPT)
        avae = n(2, "LTXVAudioVAELoader", ckpt_name=CKPT)
        te = n(3, "LTXAVTextEncoderLoader", text_encoder=TEXT_ENCODER, ckpt_name=CKPT, device="default")
        model = n(4, "LoraLoaderModelOnly", lora_name=DIST, strength_model=dist_strength, model=[ck, 0])
        VAE = [ck, 2]
    if id_lora > 0 and engine == "ltx23":  # identity LoRA on top of the distilled one (2.3 only)
        model = n(8, "LoraLoaderModelOnly", lora_name=ID_LORA, strength_model=float(id_lora), model=[model, 0])
    pos = n(5, "CLIPTextEncode", text="PROMPT", clip=[te, 0])
    neg = n(6, "CLIPTextEncode", text=NEGATIVE, clip=[te, 0])
    cond = n(7, "LTXVConditioning", frame_rate=float(FPS), positive=[pos, 0], negative=[neg, 0])

    # keyframe images (loaded once, resized per stage)
    loads = {}
    for j, k in enumerate(keys):
        if k != "real_end":  # the end frame reuses the real frame's image
            loads[k] = n(100 + j, "LoadImage", image="real.png" if k == "real" else f"kf_{k}.png")

    def prep(k, w, h, base):
        rs = n(base, "ResizeImageMaskNode", **{"resize_type": "scale dimensions", "resize_type.width": w,
                                               "resize_type.height": h, "resize_type.crop": "center",
                                               "scale_method": "lanczos",
                                               "input": [loads["real" if arcs.is_real(k) else k], 0]})
        return n(base + 1, "LTXVPreprocess", img_compression=18, image=[rs, 0])

    def guides(stage, w, h, p_, n_, lat):
        """Real frame first (in place when it is at frame 0), every other key as an LTXVAddGuide."""
        for j, k in enumerate(keys):
            img = prep(k, w, h, 200 + stage * 100 + j * 4)
            if k == "real" and idx[k] == 0:
                lat = [n(202 + stage * 100 + j * 4, "LTXVImgToVideoInplace", vae=VAE, image=[img, 0],
                         latent=lat, strength=1.0, bypass=False), 0]
            else:
                g = n(203 + stage * 100 + j * 4, "LTXVAddGuide", positive=p_, negative=n_, vae=VAE,
                      latent=lat, image=[img, 0], frame_idx=idx[k],
                      strength=1.0 if arcs.is_real(k) else guide_strength)
                p_, n_, lat = [g, 0], [g, 1], [g, 2]
        return p_, n_, lat

    if engine == "ltx25":
        # single stage at full resolution (the 2.5 template's layout: dual-CFG guider, euler ancestral, 8 sigmas)
        lat0 = n(10, "EmptyLTXVLatentVideo", width=width, height=height, length=length, batch_size=1)
        p1, n1, v1 = guides(0, width, height, [cond, 0], [cond, 1], [lat0, 0])
        alat = n(11, "LTXVEmptyLatentAudio", frames_number=length, frame_rate=float(FPS), batch_size=1, audio_vae=[avae, 0])
        av1 = n(12, "LTXVConcatAVLatent", video_latent=v1, audio_latent=[alat, 0])
        noise1 = n(13, "RandomNoise", noise_seed=42)
        g1 = n(14, "LTXVDualCFGGuider", video_cfg=1.0, audio_cfg=1.0, model=[model, 0], positive=p1, negative=n1)
        s1 = n(15, "SamplerCustomAdvanced", noise=[noise1, 0], guider=[g1, 0],
               sampler=[n(16, "SamplerEulerAncestral", eta=1.0, s_noise=1.0), 0],
               sigmas=[n(17, "ManualSigmas", sigmas=SIG8), 0], latent_image=[av1, 0])
        sep2 = n(18, "LTXVSeparateAVLatent", av_latent=[s1, 0])
        crop2 = n(19, "LTXVCropGuides", positive=p1, negative=n1, latent=[sep2, 0])
        noise2 = n(33, "RandomNoise", noise_seed=43)  # unused by 2.5, kept so the seed binding is the same
        dec = n(60, "VAEDecodeTiled", tile_size=512, overlap=64, temporal_size=64, temporal_overlap=16,
                samples=[crop2, 2], vae=VAE)
    else:
        # stage 1: half resolution
        lat0 = n(10, "EmptyLTXVLatentVideo", width=width // 2, height=height // 2, length=length, batch_size=1)
        p1, n1, v1 = guides(0, width // 2, height // 2, [cond, 0], [cond, 1], [lat0, 0])
        alat = n(11, "LTXVEmptyLatentAudio", frames_number=length, frame_rate=float(FPS), batch_size=1, audio_vae=[avae, 0])
        av1 = n(12, "LTXVConcatAVLatent", video_latent=v1, audio_latent=[alat, 0])
        noise1 = n(13, "RandomNoise", noise_seed=42)
        g1 = n(14, "CFGGuider", cfg=1, model=[model, 0], positive=p1, negative=n1)
        s1 = n(15, "SamplerCustomAdvanced", noise=[noise1, 0], guider=[g1, 0],
               sampler=[n(16, "KSamplerSelect", sampler_name="euler_ancestral_cfg_pp"), 0],
               sigmas=[n(17, "ManualSigmas", sigmas=SIG8), 0], latent_image=[av1, 0])
        sep1 = n(18, "LTXVSeparateAVLatent", av_latent=[s1, 0])
        crop1 = n(19, "LTXVCropGuides", positive=p1, negative=n1, latent=[sep1, 0])

        # stage 2: x2 latent upscale, the same keyframes again at full resolution, 3 refining steps
        up = n(30, "LTXVLatentUpsampler", samples=[crop1, 2],
               upscale_model=[n(31, "LatentUpscaleModelLoader", model_name=UPSCALER), 0], vae=VAE)
        p2, n2, v2 = guides(1, width, height, [crop1, 0], [crop1, 1], [up, 0])
        av2 = n(32, "LTXVConcatAVLatent", video_latent=v2, audio_latent=[sep1, 1])
        noise2 = n(33, "RandomNoise", noise_seed=43)
        g2 = n(34, "CFGGuider", cfg=1, model=[model, 0], positive=p2, negative=n2)
        s2 = n(35, "SamplerCustomAdvanced", noise=[noise2, 0], guider=[g2, 0],
               sampler=[n(36, "KSamplerSelect", sampler_name="euler"), 0],
               sigmas=[n(37, "ManualSigmas", sigmas=SIG3), 0], latent_image=[av2, 0])
        sep2 = n(38, "LTXVSeparateAVLatent", av_latent=[s2, 0])
        crop2 = n(39, "LTXVCropGuides", positive=p2, negative=n2, latent=[sep2, 0])

        dec = n(60, "VAEDecodeTiled", tile_size=768, overlap=64, temporal_size=4096, temporal_overlap=4,
                samples=[crop2, 2], vae=VAE)
    frames = dec
    if color_match > 0:
        ref = n(64, "ResizeImageMaskNode", **{"resize_type": "scale dimensions", "resize_type.width": width,
                                              "resize_type.height": height, "resize_type.crop": "center",
                                              "scale_method": "lanczos", "input": [loads["real"], 0]})
        frames = n(65, "ColorMatch", image_ref=[ref, 0], image_target=[dec, 0], method="mkl",
                   strength=float(color_match), multithread=True)
    adec = n(61, "LTXVAudioVAEDecode", samples=[sep2, 1], audio_vae=[avae, 0])
    cv = n(62, "CreateVideo", fps=float(FPS), images=[frames, 0], audio=[adec, 0])
    n(63, "SaveVideo", filename_prefix="video/bullettime_orbit", format="auto", codec="auto", video=[cv, 0])

    bindings = {"prompt": [[pos, "text"]], "seed": [[noise1, "noise_seed"], [noise2, "noise_seed"]]}
    for k, node in loads.items():
        bindings[k] = [[node, "image"]]
    return W, bindings


def orbit_size(width: int, height: int) -> tuple[int, int]:
    """LTX output size for a source: 704x1280 portrait, 1280x704 landscape, 960x960 square-ish."""
    r = width / height
    if r < 0.8:
        return 704, 1280
    if r > 1.25:
        return 1280, 704
    return 960, 960


def stills_workflow(views: list[str] | None = None, quality: bool = False) -> tuple[dict, dict, dict]:
    """-> (workflow, bindings, output-node -> view name) for the Qwen views job.
    views: a subset of arcs.STILL_VIEWS keeps only those branches (re-rolling one bad view costs ~1/8 of a job).
    quality: the template's quality switch, 40 steps at cfg 4 instead of the Lightning 4-step LoRA."""
    wf = json.loads((WF_DIR / "orbit_stills_api.json").read_text())
    raw = json.loads((WF_DIR / "orbit_stills_bindings.json").read_text())
    bindings = {k: v for k, v in raw.items() if not k.startswith("_")}
    e_to_view = {f"e{i + 1}": v for i, v in enumerate(arcs.STILL_VIEWS)}
    rename = {node: e_to_view[e] for node, e in raw["_outputs"].items()}
    if views is not None:
        keep = set(views)
        for save, view in list(rename.items()):
            if view not in keep:
                prefix = wf[save]["inputs"]["images"][0].rsplit(":", 1)[0]  # the branch subgraph feeding it
                loads = {v_[0] for k_, v_ in wf.items() if k_.startswith(prefix + ":")
                         for v_ in v_["inputs"].values() if isinstance(v_, list) and ":" not in v_[0]}
                for nid in [k_ for k_ in wf if k_.startswith(prefix + ":")] + [save] + sorted(loads):
                    wf.pop(nid, None)
                del rename[save]
        for k in list(bindings):
            bindings[k] = [pair for pair in comfy._pairs(bindings[k]) if pair[0] in wf]
            if not bindings[k]:
                del bindings[k]
    if quality:
        for node in wf.values():
            if node["class_type"] == "PrimitiveBoolean":
                node["inputs"]["value"] = False
    return wf, bindings, rename


def stills_values(image: str, seed: int | None = None, distance: str = arcs.DISTANCE,
                  views: list[str] | None = None) -> dict:
    vals = {"image": image, "angle_lora": arcs.ANGLE_LORA}
    for i, v in enumerate(arcs.STILL_VIEWS):
        if views is not None and v not in views:
            continue
        vals[f"p{i + 1}"] = arcs.view_prompt(v, distance)
        if seed is not None:
            vals[f"s{i + 1}"] = seed + i
    return vals


# SeedVR2 (open weights, Apache-2.0): video upscaler available on Comfy Cloud. 3B fp8 is the fast one.
SEEDVR2_DIT = "seedvr2_ema_3b_fp8_e4m3fn.safetensors"
SEEDVR2_VAE = "ema_vae_fp16.safetensors"


def build_upscale(video: str = "orbit.mp4", short_side: int = 1080, batch: int = 5,
                  dit: str = SEEDVR2_DIT) -> tuple[dict, dict]:
    """-> (API workflow, bindings): SeedVR2 upscales the orbit clip (704x1280 -> short side `short_side`),
    keeping its audio. batch must be 4n+1; bigger = more temporal consistency, more VRAM."""
    W: dict = {}

    def n(i, cls, **inp):
        W[str(i)] = {"class_type": cls, "inputs": inp}
        return str(i)

    vid = n(1, "LoadVideo", file=video)
    comp = n(2, "GetVideoComponents", video=[vid, 0])
    d = n(3, "SeedVR2LoadDiTModel", model=dit, device="cuda:0")
    v = n(4, "SeedVR2LoadVAEModel", model=SEEDVR2_VAE, device="cuda:0")
    up = n(5, "SeedVR2VideoUpscaler", image=[comp, 0], dit=[d, 0], vae=[v, 0], seed=42, resolution=short_side,
           max_resolution=0, batch_size=batch, uniform_batch_size=True, color_correction="lab")
    cv = n(6, "CreateVideo", fps=[comp, 2], images=[up, 0], audio=[comp, 1])
    n(7, "SaveVideo", filename_prefix="video/bullettime_orbit_up", format="auto", codec="auto", video=[cv, 0])
    return W, {"video": [[vid, "file"]], "seed": [[up, "seed"]]}


if __name__ == "__main__":
    import sys
    arc = sys.argv[1] if len(sys.argv) > 1 else arcs.DEFAULT_ARC
    w, b = build_orbit(arc)
    w["5"]["inputs"]["text"] = orbit_prompt(arc)
    print(json.dumps(w, indent=1))
