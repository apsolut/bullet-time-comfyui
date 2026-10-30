"""One project = one take in a folder. Steps (each can be re-run on its own):

    new_project(video)   copy + probe + pick the freeze moment                        free
    stills(t)            frozen frame -> 8 views around the person (1 Qwen job)       ~$0.045
    orbit(arc)           keyframes -> LTX-2.3 camera move through frozen time (1 job)  ~$0.055
    render()             ramp + orbit + rewind spliced into the real take (ffmpeg)    free

state.json holds no secrets: the Comfy key is passed per call and never stored.
"""
from __future__ import annotations

import json
import random
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable

from . import arcs, assemble, comfy, orient, pick, workflows
from .util import probe, run

Log = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


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


def check_view(view: str, still: Path, real: Path) -> tuple[bool, str]:
    """Reject a still that came back in the wrong orientation, or with an upright source face lying sideways."""
    try:
        if orient.is_portrait(orient.image_size(still)) != orient.is_portrait(orient.image_size(real)):
            return False, "wrong orientation"
    except Exception as e:  # noqa: BLE001
        return False, f"unreadable ({e})"
    if view in arcs.FACE_VIEWS and orient.face_roll(real) == 0 and orient.face_roll(still) in (90, 270):
        return False, "subject rotated 90 degrees"
    return True, "ok"


def stills(p: Project, t: float | None = None, key=None, seed: int | None = None, log: Log = _noop,
           on_status: Callable[[dict], None] | None = None) -> dict:
    t = p.state["t"] if t is None else float(t)
    real = freeze_frame(p, t)
    wf, bindings, rename = workflows.stills_workflow()
    seed = random.randint(0, 2**40) if seed is None else seed
    out_dir = p.path(f"stills/{real.stem}_{seed}")
    # same framing as the real frame: a "medium shot" view of a selfie close-up makes the orbit zoom out
    distance = orient.shot_distance(real) or arcs.DISTANCE
    log(f"stills: 1 Qwen job, 8 views of the frame at {t:.2f}s ({distance})")
    rec = comfy.run_workflow(wf, bindings, workflows.stills_values(str(real), seed, distance), out_dir, key,
                             on_status=on_status, rename=rename)
    if rec["status"] != "success":
        raise comfy.ComfyError(f"stills job failed: {rec.get('error', rec['status'])}")
    views = {}
    for f in rec["outputs"]:
        v = Path(f).stem
        if v in arcs.VIEWS:
            ok, why = check_view(v, Path(f), real)
            views[v] = {"path": str(Path(f).relative_to(p.dir)), "ok": ok, "reason": why}
    p.state.update(t=t, real=str(real.relative_to(p.dir)),
                   stills={"t": t, "seed": seed, "distance": distance, "prompt_id": rec["prompt_id"], "views": views})
    p.state.setdefault("prompt_ids", []).append(rec["prompt_id"])
    p.save()
    return p.state["stills"]


def usable_views(p: Project) -> set[str]:
    st = p.state.get("stills") or {}
    return {v for v, r in (st.get("views") or {}).items() if r.get("ok")}


def orbit(p: Project, arc: str = arcs.DEFAULT_ARC, key=None, seed: int | None = None, subject: str = "",
          sound: str = "whoosh", log: Log = _noop, on_status: Callable[[dict], None] | None = None,
          guide: float = workflows.GUIDE_STRENGTH) -> dict:
    if arc not in arcs.ARCS:
        raise ValueError(f"unknown arc {arc!r} (one of {', '.join(arcs.ARCS)})")
    st = p.state.get("stills")
    if not st or abs(st["t"] - p.state["t"]) > 1e-3:
        raise ValueError("make the stills for this freeze moment first")
    ok = usable_views(p)
    if not arcs.available(arc, ok):
        raise ValueError(f"{arc}: its end view has no usable still; re-roll the stills or pick another arc")
    keys = [k for k in arcs.ARCS[arc]["keys"] if k == "real" or k in ok]
    w, h = workflows.orbit_size(p.state["info"]["width"], p.state["info"]["height"])
    wf, bindings = workflows.build_orbit(arc, w, h, guide_strength=guide, keys=keys)
    seed = random.randint(0, 2**48) if seed is None else seed
    values = {"prompt": workflows.orbit_prompt(arc, subject, sound), "seed": seed, "real": str(p.path(p.state["real"]))}
    for k in keys:
        if k != "real":
            values[k] = str(p.path(st["views"][k]["path"]))
    out_dir = p.path(f"orbits/{arc}_{seed}")
    dropped = [k for k in arcs.ARCS[arc]["keys"] if k not in keys]
    log(f"orbit: 1 LTX-2.3 job, {arc}, {arcs.frames(arc)} frames, keys {keys}" + (f" (dropped {dropped})" if dropped else ""))
    rec = comfy.run_workflow(wf, bindings, values, out_dir, key, on_status=on_status)
    vids = [o for o in rec.get("outputs", []) if o.lower().endswith(comfy.VIDEO_EXT)]
    if rec["status"] != "success" or not vids:
        raise comfy.ComfyError(f"orbit job failed: {rec.get('error', rec['status'])}")
    clip = Path(vids[0]).rename(out_dir / "orbit.mp4")
    p.state["orbit"] = {"arc": arc, "seed": seed, "keys": keys, "dropped": dropped, "subject": subject, "sound": sound,
                        "guide": guide, "prompt_id": rec["prompt_id"], "path": str(clip.relative_to(p.dir)), "t": p.state["t"]}
    p.state.setdefault("prompt_ids", []).append(rec["prompt_id"])
    p.save()
    return p.state["orbit"]


def render(p: Project, ramp_s: float = 0.4, ramp_slow: float = 3.0, sound: str | None = None, log: Log = _noop) -> dict:
    o = p.state.get("orbit")
    if not o:
        raise ValueError("generate the orbit first")
    log("render: ffmpeg (free)")
    rep = assemble.render(p.take, p.path(o["path"]), o["t"], o["arc"], p.path("bullettime.mp4"), ramp_s=ramp_s,
                          ramp_slow=ramp_slow, sound=sound or o.get("sound", "whoosh"),
                          orbit_only=p.path("bullettime_loop.mp4"))
    rep["out"], rep["orbit_only"] = "bullettime.mp4", "bullettime_loop.mp4"
    rep["rendered"] = time.time()
    p.state["render"] = rep
    p.save()
    return rep


def estimate(p: Project, need_stills: bool | None = None) -> dict:
    st = p.state.get("stills")
    if need_stills is None:
        need_stills = not st or abs(st["t"] - p.state["t"]) > 1e-3
    jobs = (["stills"] if need_stills else []) + ["orbit"]
    return {"jobs": jobs, "usd": round(sum(comfy.job_cost(j) for j in jobs), 3)}
