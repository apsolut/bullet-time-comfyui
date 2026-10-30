"""One project = one take in a folder. Steps (each can be re-run on its own):

    new_project(video)   copy + probe + pick the freeze moment                        free
    stills(t)            frozen frame -> 8 views around the person (1 Qwen job)       ~$0.045 (quality: ~$0.25)
    retry_views()        re-roll only the rejected views with new seeds               ~$0.025 per view
    orbit(arc)           keyframes -> LTX-2.3 camera move through frozen time (1 job)  ~$0.055
    variants(arc, n)     n orbits with different seeds at once, pick the best          n x orbit
    upscale()            SeedVR2 upscales the chosen orbit to 1080p+ (1 job)          ~$0.12
    render()             ramp + orbit + rewind spliced into the real take (ffmpeg)    free
    export()             studio bundle: ProRes master, loop, frame, views, timeline   free

Every paid job is written to state["ledger"] with the Comfy Cloud balance before and after it.

state.json holds no secrets: the Comfy key is passed per call and never stored.
"""
from __future__ import annotations

import json
import random
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import arcs, assemble, comfy, eyes, orient, pick, workflows
from .util import probe, run

Log = Callable[[str], None]
_state_lock = threading.RLock()  # variants() runs orbit() in threads: state updates are serialized


def _noop(_msg: str) -> None:
    pass


def _paid(p: "Project", kind: str, key, fn):
    """Run one paid Comfy job and book it: balance before/after (both free calls) go to state["ledger"]."""
    before = comfy.balance_usd(key)
    t0 = time.time()
    rec = fn()
    after = comfy.balance_usd(key)
    entry = {"kind": kind, "prompt_id": rec.get("prompt_id"), "status": rec.get("status"),
             "seconds": round(time.time() - t0, 1), "balance_before": before, "balance_after": after,
             "usd": round(before - after, 4) if before is not None and after is not None else None,
             "at": time.time()}
    with _state_lock:
        p.state.setdefault("ledger", []).append(entry)
        if rec.get("prompt_id"):
            p.state.setdefault("prompt_ids", []).append(rec["prompt_id"])
        p.save()
    return rec


class Project:
    def __init__(self, root: str | Path):
        self.dir = Path(root)
        self.state_path = self.dir / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    @property
    def id(self) -> str:
        return self.dir.name

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def path(self, name: str) -> Path:
        return self.dir / name

    @property
    def take(self) -> Path:
        return self.path(self.state["take"])


def new_project(video: str | Path, base: str | Path, pid: str | None = None) -> Project:
    p = Project(Path(base) / (pid or uuid.uuid4().hex[:16]))
    p.dir.mkdir(parents=True, exist_ok=True)
    src = Path(video)
    dst = p.path("take" + (src.suffix.lower() or ".mp4"))
    if src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    info = probe(dst)
    if "width" not in info:
        raise ValueError("no video stream in the upload")
    p.state = {"id": p.id, "created": time.time(), "take": dst.name, "info": info, "status": "ready"}
    p.state["analysis"] = pick.analyze(dst)
    p.state["t"] = p.state["analysis"]["t"]
    p.save()
    return p


def freeze_frame(p: Project, t: float) -> Path:
    """The exact frame at t, upright, long side <= 1536 (what Qwen and LTX get)."""
    fps = p.state["info"].get("fps") or 30.0
    t = round(round(t * fps) / fps, 4)
    out = p.path(f"frames/real_{int(round(t * 1000)):06d}.png")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        run(["ffmpeg", "-y", "-i", str(p.take), "-ss", f"{t:.4f}", "-frames:v", "1",
             "-vf", "scale='if(gt(iw,ih),min(1536,iw),-2)':'if(gt(iw,ih),-2,min(1536,ih))'", str(out)])
    return out


EYE_VIEWS = {"fr", "fl", "up"}  # views where both eyes face the camera


def check_view(view: str, still: Path, real: Path) -> tuple[bool, str]:
    """Reject a still that came back in the wrong orientation, or with an upright source face lying sideways."""
    try:
        if orient.is_portrait(orient.image_size(still)) != orient.is_portrait(orient.image_size(real)):
            return False, "wrong orientation"
    except Exception as e:  # noqa: BLE001
        return False, f"unreadable ({e})"
    if view in arcs.FACE_VIEWS and orient.face_roll(real) == 0 and orient.face_roll(still) in (90, 270):
        return False, "subject rotated 90 degrees"
    if view in EYE_VIEWS:  # Qwen can still close the eyes; a closed-eyes keyframe pulls the orbit's face with it
        b = eyes.blink_image(still)
        if b is not None and b > 0.6:  # looser than the take: a generated squint (~0.5) still reads as open
            return False, f"eyes closed (blink {b:.2f})"
    return True, "ok"


def stills(p: Project, t: float | None = None, key=None, seed: int | None = None, log: Log = _noop,
           on_status: Callable[[dict], None] | None = None, quality: bool = False) -> dict:
    t = p.state["t"] if t is None else float(t)
    fps = p.state["info"].get("fps") or 30.0
    f = int(round(t * fps))
    f_open = eyes.nearest_open((p.state.get("analysis") or {}).get("blink"), f)
    if f_open != f:
        log(f"eyes closed at {t:.2f}s, freezing {f_open - f:+d} frames later at {f_open / fps:.2f}s instead")
        t = f_open / fps
    real = freeze_frame(p, t)
    wf, bindings, rename = workflows.stills_workflow(quality=quality)
    seed = random.randint(0, 2**40) if seed is None else seed
    out_dir = p.path(f"stills/{real.stem}_{seed}")
    # same framing as the real frame: a "medium shot" view of a selfie close-up makes the orbit zoom out
    distance = orient.shot_distance(real) or arcs.DISTANCE
    log(f"stills: 1 Qwen job, 8 views of the frame at {t:.2f}s ({distance}{', quality' if quality else ''})")
    rec = _paid(p, "stills_quality" if quality else "stills", key,
                lambda: comfy.run_workflow(wf, bindings, workflows.stills_values(str(real), seed, distance), out_dir,
                                           key, on_status=on_status, rename=rename))
    if rec["status"] != "success":
        raise comfy.ComfyError(f"stills job failed: {rec.get('error', rec['status'])}")
    views = {}
    for f in rec["outputs"]:
        v = Path(f).stem
        if v in arcs.VIEWS:
            ok, why = check_view(v, Path(f), real)
            views[v] = {"path": str(Path(f).relative_to(p.dir)), "ok": ok, "reason": why, "seed": seed}
    with _state_lock:
        p.state.update(t=t, real=str(real.relative_to(p.dir)),
                       stills={"t": t, "seed": seed, "distance": distance, "quality": quality,
                               "prompt_id": rec["prompt_id"], "views": views})
        p.state.pop("orbit", None); p.state.pop("orbits", None); p.state.pop("render", None)  # from older views
        p.save()
    return p.state["stills"]


def retry_views(p: Project, views: list[str] | None = None, key=None, seed: int | None = None, log: Log = _noop,
                on_status: Callable[[dict], None] | None = None, tries: int = 2) -> dict:
    """Re-roll the rejected views (or the given ones) with new seeds, in one reduced Qwen job per try, until
    they pass the checks. Costs ~$0.025 per view and try (the model load dominates)."""
    st = p.state.get("stills")
    if not st:
        raise ValueError("make the stills first")
    todo = views or [v for v, r in st["views"].items() if not r["ok"]]
    todo = [v for v in todo if v in arcs.VIEWS]
    real = p.path(p.state["real"])
    for n in range(tries):
        if not todo:
            break
        seed_n = (random.randint(0, 2**40) if seed is None else seed + 100 * (n + 1))
        # "right side view ... close-up" lays the subject sideways on portrait frames in every seed we tried
        # (Oct 2026); the same view at "medium shot" comes back upright. So the retry steps the distance back.
        distance = st["distance"] if n == 0 else arcs.wider(st["distance"])
        wf, bindings, rename = workflows.stills_workflow(views=todo, quality=st.get("quality", False))
        out_dir = p.path(f"stills/{real.stem}_{seed_n}")
        log(f"re-rolling view{'s' if len(todo) > 1 else ''} {', '.join(todo)} (try {n + 1}/{tries}, seed {seed_n}, {distance})")
        rec = _paid(p, "view", key, lambda: comfy.run_workflow(
            wf, bindings, workflows.stills_values(str(real), seed_n, distance, views=todo), out_dir, key,
            on_status=on_status, rename=rename))
        if rec["status"] != "success":
            raise comfy.ComfyError(f"view job failed: {rec.get('error', rec['status'])}")
        still_bad = []
        for f in rec["outputs"]:
            v = Path(f).stem
            if v not in todo:
                continue
            ok, why = check_view(v, Path(f), real)
            if ok or not st["views"].get(v, {}).get("ok"):
                st["views"][v] = {"path": str(Path(f).relative_to(p.dir)), "ok": ok, "reason": why, "seed": seed_n,
                                  "distance": distance}
            if not ok:
                still_bad.append(v)
            log(f"  {v}: {'ok' if ok else why}")
        todo = still_bad
        with _state_lock:
            p.save()
    return st


def usable_views(p: Project) -> set[str]:
    st = p.state.get("stills") or {}
    return {v for v, r in (st.get("views") or {}).items() if r.get("ok")}


def orbit(p: Project, arc: str = arcs.DEFAULT_ARC, key=None, seed: int | None = None, subject: str = "",
          sound: str = "whoosh", log: Log = _noop, on_status: Callable[[dict], None] | None = None,
          guide: float = workflows.GUIDE_STRENGTH, color_match: float = 0.0, select: bool = True) -> dict:
    """One LTX orbit job. select=False only records it under state["orbits"] (see variants())."""
    if arc not in arcs.ARCS:
        raise ValueError(f"unknown arc {arc!r} (one of {', '.join(arcs.ARCS)})")
    st = p.state.get("stills")
    if not st or abs(st["t"] - p.state["t"]) > 1e-3:
        raise ValueError("make the stills for this freeze moment first")
    ok = usable_views(p)
    if not arcs.available(arc, ok):
        raise ValueError(f"{arc}: its end view has no usable still; re-roll the stills or pick another arc")
    keys = [k for k in arcs.ARCS[arc]["keys"] if arcs.is_real(k) or k in ok]
    w, h = workflows.orbit_size(p.state["info"]["width"], p.state["info"]["height"])
    wf, bindings = workflows.build_orbit(arc, w, h, guide_strength=guide, keys=keys, color_match=color_match)
    seed = random.randint(0, 2**48) if seed is None else seed
    values = {"prompt": workflows.orbit_prompt(arc, subject, sound), "seed": seed, "real": str(p.path(p.state["real"]))}
    for k in keys:
        if not arcs.is_real(k):
            values[k] = str(p.path(st["views"][k]["path"]))
    out_dir = p.path(f"orbits/{arc}_{seed}")
    dropped = [k for k in arcs.ARCS[arc]["keys"] if k not in keys]
    log(f"orbit: 1 LTX-2.3 job, {arc}, {arcs.frames(arc)} frames, keys {keys}" + (f" (dropped {dropped})" if dropped else ""))
    rec = _paid(p, "orbit", key, lambda: comfy.run_workflow(wf, bindings, values, out_dir, key, on_status=on_status))
    vids = [o for o in rec.get("outputs", []) if o.lower().endswith(comfy.VIDEO_EXT)]
    if rec["status"] != "success" or not vids:
        raise comfy.ComfyError(f"orbit job failed: {rec.get('error', rec['status'])}")
    clip = Path(vids[0]).rename(out_dir / "orbit.mp4")
    o = {"arc": arc, "seed": seed, "keys": keys, "dropped": dropped, "subject": subject, "sound": sound,
         "guide": guide, "color_match": color_match, "prompt_id": rec["prompt_id"],
         "path": str(clip.relative_to(p.dir)), "t": p.state["t"], "made": time.time()}
    with _state_lock:
        orbits = [x for x in p.state.get("orbits", []) if x.get("path") != o["path"]]
        p.state["orbits"] = orbits + [o]
        if select:
            p.state["orbit"] = o
            p.state.pop("render", None)
        p.save()
    return o


def variants(p: Project, arc: str = arcs.DEFAULT_ARC, n: int = 3, key=None, seed: int | None = None, log: Log = _noop,
             on_status: Callable[[int, dict], None] | None = None, **kw) -> list[dict]:
    """n orbits of the same arc with different seeds, submitted at once (comfy.MAX_JOBS in flight). The first
    one becomes the current orbit; select_orbit() switches. Costs n x orbit."""
    n = max(1, min(int(n), 6))
    base = random.randint(0, 2**40) if seed is None else seed
    log(f"variants: {n} x {arc} orbit jobs in parallel")

    def one(i):
        cb = (lambda rec: on_status(i, rec)) if on_status else None
        return orbit(p, arc, key=key, seed=base + i * 7919, log=log, on_status=cb, select=False, **kw)
    with ThreadPoolExecutor(max_workers=n) as ex:
        out = list(ex.map(one, range(n)))
    select_orbit(p, out[0]["seed"], out[0]["arc"])
    return out


def select_orbit(p: Project, seed: int, arc: str | None = None) -> dict:
    """Make one of state["orbits"] the current orbit (what render() splices)."""
    for o in p.state.get("orbits", []):
        if o["seed"] == seed and (arc is None or o["arc"] == arc):
            with _state_lock:
                p.state["orbit"] = o
                p.state.pop("render", None)
                p.save()
            return o
    raise ValueError("no such orbit")


def upscale(p: Project, key=None, short_side: int = 1080, log: Log = _noop,
            on_status: Callable[[dict], None] | None = None) -> dict:
    """SeedVR2 (open weights) upscales the current orbit clip; render() then uses the upscaled clip."""
    o = p.state.get("orbit")
    if not o:
        raise ValueError("generate the orbit first")
    wf, bindings = workflows.build_upscale(short_side=short_side)
    src = p.path(o["path"])
    out_dir = src.parent
    log(f"upscale: 1 SeedVR2 job, short side {short_side}px")
    rec = _paid(p, "upscale", key, lambda: comfy.run_workflow(
        wf, bindings, {"video": str(src), "seed": o["seed"] % (2**32)}, out_dir, key, on_status=on_status))
    vids = [v for v in rec.get("outputs", []) if v.lower().endswith(comfy.VIDEO_EXT) and Path(v).name != "orbit.mp4"]
    if rec["status"] != "success" or not vids:
        raise comfy.ComfyError(f"upscale job failed: {rec.get('error', rec['status'])}")
    up = Path(vids[0]).rename(out_dir / "orbit_up.mp4")
    with _state_lock:
        o["path_up"] = str(up.relative_to(p.dir))
        o["upscale"] = {"short_side": short_side, "prompt_id": rec["prompt_id"], "model": workflows.SEEDVR2_DIT}
        for x in p.state.get("orbits", []):
            if x["path"] == o["path"]:
                x.update(path_up=o["path_up"], upscale=o["upscale"])
        p.state.pop("render", None)
        p.save()
    return o


def render(p: Project, ramp_s: float = 0.4, ramp_slow: float = 3.0, sound: str | None = None, log: Log = _noop,
           use_upscaled: bool = True) -> dict:
    o = p.state.get("orbit")
    if not o:
        raise ValueError("generate the orbit first")
    clip = p.path(o["path_up"]) if use_upscaled and o.get("path_up") else p.path(o["path"])
    log(f"render: ffmpeg (free){' from the upscaled orbit' if clip.name == 'orbit_up.mp4' else ''}")
    rep = assemble.render(p.take, clip, o["t"], o["arc"], p.path("bullettime.mp4"), ramp_s=ramp_s,
                          ramp_slow=ramp_slow, sound=sound or o.get("sound", "whoosh"),
                          orbit_only=p.path("bullettime_loop.mp4"))
    rep["out"], rep["orbit_only"] = "bullettime.mp4", "bullettime_loop.mp4"
    rep["orbit_clip"] = str(clip.relative_to(p.dir))
    rep["ramp_s"], rep["ramp_slow"], rep["rendered"] = ramp_s, ramp_slow, time.time()
    with _state_lock:
        p.state["render"] = rep
        p.save()
    return rep


def export(p: Project, master: bool = True, log: Log = _noop) -> Path:
    """Studio bundle -> export/bullettime_<id>.zip: ProRes 422 HQ master (.mov) + the H.264 delivery, the
    freeze loop, the raw orbit clip(s), the frozen frame and every view, timeline.json (segments with source
    in/out points for an NLE), the ledger, and the exact ComfyUI graphs that were submitted."""
    rep = p.state.get("render")
    if not rep:
        raise ValueError("render first")
    ex = p.path("export")
    ex.mkdir(exist_ok=True)
    files = {"bullettime.mp4": p.path(rep["out"]), "bullettime_loop.mp4": p.path(rep["orbit_only"]),
             "frozen_frame.png": p.path(p.state["real"])}
    if master:
        mov = ex / "bullettime_master.mov"
        if not mov.exists() or mov.stat().st_mtime < p.path(rep["out"]).stat().st_mtime:
            log("export: ProRes 422 HQ master")
            run(["ffmpeg", "-y", "-i", str(p.path(rep["out"])), "-c:v", "prores_ks", "-profile:v", "3",
                 "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s16le", str(mov)])
        files["bullettime_master.mov"] = mov
    o = p.state.get("orbit") or {}
    for k in ("path", "path_up"):
        if o.get(k):
            files[f"orbit/{Path(o[k]).name}"] = p.path(o[k])
    for v, r in (p.state.get("stills") or {}).get("views", {}).items():
        files[f"views/{v}{'' if r['ok'] else '_rejected'}.png"] = p.path(r["path"])
    timeline = {"project": p.id, "take": p.state["take"], "freeze_at": rep["freeze_at"], "arc": rep["arc"],
                "fps": rep["fps"], "size": [rep["width"], rep["height"]], "duration": rep["duration"],
                "orbit_clip": rep.get("orbit_clip"), "segments": rep["segments"],
                "note": "speed = source seconds per output second; orbit segments index into the orbit clip"}
    (ex / "timeline.json").write_text(json.dumps(timeline, indent=2))
    (ex / "ledger.json").write_text(json.dumps(p.state.get("ledger", []), indent=2))
    graphs = comfy.export_workflows(p.state.get("prompt_ids", []), ex / "workflows")
    zpath = ex / f"bullettime_{p.id}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for name, src in files.items():
            if Path(src).exists():
                z.write(src, name)
        z.write(ex / "timeline.json", "timeline.json")
        z.write(ex / "ledger.json", "ledger.json")
        for g in graphs:
            z.write(g, f"workflows/{g.name}")
    log(f"export: {zpath}")
    return zpath


def spent_usd(p: Project) -> float:
    return round(sum(e.get("usd") or 0.0 for e in p.state.get("ledger", []) if (e.get("usd") or 0) > 0), 3)


def estimate(p: Project, need_stills: bool | None = None, quality: bool = False, n_variants: int = 1,
             upscale: bool = False) -> dict:
    st = p.state.get("stills")
    if need_stills is None:
        need_stills = not st or abs(st["t"] - p.state["t"]) > 1e-3
    jobs = (["stills_quality" if quality else "stills"] if need_stills else []) + ["orbit"] * max(1, int(n_variants))
    if upscale:
        jobs.append("upscale")
    return {"jobs": jobs, "usd": round(sum(comfy.job_cost(j) for j in jobs), 3), "spent_usd": spent_usd(p)}
