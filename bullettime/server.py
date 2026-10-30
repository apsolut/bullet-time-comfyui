"""Web app and HTTP API: `bullettime serve` -> http://127.0.0.1:8790 (OpenAPI at /api/docs)

The Comfy Cloud key lives in the browser (localStorage) and arrives per request in `X-Comfy-Key`. It is
kept in memory for that one action only: never written to disk, logged or returned. (A local server is
needed because Comfy Cloud's API sends no CORS headers, so a page can't call it directly.)

Studio deployment (one box for the team, see README "Production"):
  COMFY_API_KEY          server-side key, so nobody has to paste one (browser key still wins if given)
  BULLETTIME_PASSWORD    shared password: every /api call must carry it in `X-App-Password`
  BULLETTIME_MAX_JOBS    Comfy jobs in flight at once (default 3)
  BULLETTIME_DATA        data folder (projects, workflow archive); `bullettime serve --data` overrides
"""

import io
import json
import os
import shutil
import threading
import time
import traceback
import zipfile
from pathlib import Path

from . import arcs, comfy, eyes, pipeline, workflows

WEB = Path(__file__).resolve().parent / "web"
MAX_UPLOAD_MB = int(os.environ.get("BULLETTIME_MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_S = float(os.environ.get("BULLETTIME_MAX_UPLOAD_S", "30"))
VERSION = "0.2.0"


def create_app(data_dir: str | Path):
    from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from pydantic import BaseModel

    data = Path(data_dir)
    projects = data / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    comfy.set_workflow_archive(data / "workflows")
    locks: dict[str, threading.Lock] = {}
    app = FastAPI(title="Bullet Time", version=VERSION, docs_url="/api/docs", openapi_url="/api/openapi.json")
    password = os.environ.get("BULLETTIME_PASSWORD") or None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if password and request.url.path.startswith("/api/") and request.url.path != "/api/health":
            if request.headers.get("x-app-password") != password:
                return JSONResponse({"detail": "wrong or missing app password"}, status_code=401)
        return await call_next(request)

    def load(pid: str) -> pipeline.Project:
        if not pid.isalnum():
            raise HTTPException(404, "no such project")
        p = pipeline.Project(projects / pid)
        if not p.state:
            raise HTTPException(404, "no such project")
        return p

    def public(p: pipeline.Project) -> dict:
        s = dict(p.state)
        s["estimate"] = pipeline.estimate(p)
        s["usable"] = sorted(pipeline.usable_views(p))
        s["arcs_ok"] = [a for a in arcs.ARCS if arcs.available(a, set(s["usable"]))] if s.get("stills") else list(arcs.ARCS)
        s["spent_usd"] = pipeline.spent_usd(p)
        if s.get("analysis"):
            s["analysis"] = {k: v for k, v in s["analysis"].items() if k != "blink"}  # big, UI doesn't need it
        return s

    def summary(p: pipeline.Project) -> dict:
        s = p.state
        return {"id": p.id, "created": s.get("created"), "duration": (s.get("info") or {}).get("duration"),
                "t": s.get("t"), "arc": (s.get("orbit") or {}).get("arc"), "rendered": bool(s.get("render")),
                "job": (s.get("job") or {}).get("status"), "spent_usd": pipeline.spent_usd(p),
                "orbits": len(s.get("orbits", []))}

    def set_job(p: pipeline.Project, **kw):
        p.state = pipeline.Project(p.dir).state or p.state
        job = dict(p.state.get("job") or {})
        job.update(kw, updated=time.time())
        p.state["job"] = job
        p.save()

    def background(p: pipeline.Project, name: str, fn):
        lock = locks.setdefault(p.id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "this project is busy, wait for the current step")
        set_job(p, name=name, step="starting", status="running", error=None, started=time.time())

        def go():
            try:
                fn()
                set_job(p, step="done", status="done")
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                set_job(p, status="error", error=str(e)[:600])
            finally:
                lock.release()
        threading.Thread(target=go, daemon=True).start()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB / "index.html").read_text()

    @app.get("/web/{name}")
    def web_file(name: str):
        f = WEB / name
        if not name.replace(".", "").replace("-", "").replace("_", "").isalnum() or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "ffmpeg": bool(shutil.which("ffmpeg")), "eyes": eyes.status(),
                "server_key": bool(comfy.env_key()), "password": bool(password), "max_jobs": comfy.MAX_JOBS,
                "costs_usd": comfy.JOB_COST_USD}

    @app.get("/api/projects")
    def list_projects(limit: int = 50):
        out = []
        for d in projects.iterdir():
            if d.is_dir() and (d / "state.json").exists():
                try:
                    out.append(summary(pipeline.Project(d)))
                except Exception:  # noqa: BLE001
                    continue
        out.sort(key=lambda x: x.get("created") or 0, reverse=True)
        return out[:max(1, min(limit, 500))]

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        p = load(pid)
        lock = locks.setdefault(p.id, threading.Lock())
        if lock.locked():
            raise HTTPException(409, "this project is busy")
        shutil.rmtree(p.dir)
        locks.pop(p.id, None)
        return {"deleted": pid}

    @app.get("/api/arcs")
    def list_arcs():
        return {k: {"label": v["label"], "keys": v["keys"], "freeze_s": arcs.freeze_seconds(k)} for k, v in arcs.ARCS.items()}

    @app.post("/api/key/check")
    def key_check(x_comfy_key: str | None = Header(None)):
        try:
            comfy.check_key(x_comfy_key)
        except comfy.ComfyError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "balance_usd": comfy.balance_usd(x_comfy_key)}

    @app.post("/api/projects")
    async def create(file: UploadFile = File(...)):
        suffix = Path(file.filename or "take.mp4").suffix.lower() or ".mp4"
        if suffix not in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
            raise HTTPException(400, "upload a video (mp4, mov, webm)")
        tmp = data / "uploads" / f"{time.time_ns()}{suffix}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(tmp, "wb") as f:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_MB << 20:
                    f.close(); tmp.unlink(missing_ok=True)
                    raise HTTPException(413, f"max {MAX_UPLOAD_MB} MB")
                f.write(chunk)
        try:
            from .util import probe
            info = probe(tmp)
            if info["duration"] > MAX_UPLOAD_S:
                raise HTTPException(400, f"keep the take under {MAX_UPLOAD_S:.0f} s (this one is {info['duration']:.1f} s)")
            p = pipeline.new_project(tmp, projects)
        except SystemExit as e:  # ffprobe/ffmpeg failure
            raise HTTPException(400, f"could not read that video: {str(e)[:300]}")
        finally:
            tmp.unlink(missing_ok=True)
        return public(p)

    @app.get("/api/projects/{pid}")
    def get(pid: str):
        return public(load(pid))

    class Freeze(BaseModel):
        t: float

    @app.patch("/api/projects/{pid}")
    def set_freeze(pid: str, body: Freeze):
        p = load(pid)
        dur = p.state["info"]["duration"]
        if not 0.1 <= body.t <= dur - 0.1:
            raise HTTPException(400, f"pick a moment between 0.1 and {dur - 0.1:.1f} s")
        p.state["t"] = round(body.t, 3)
        p.save()
        return public(p)

    def need_key(x_comfy_key):
        key = x_comfy_key or comfy.env_key()
        if not key:
            raise HTTPException(401, "paste your Comfy Cloud key first")
        return key

    class Generate(BaseModel):
        arc: str = arcs.DEFAULT_ARC
        subject: str = ""
        sound: str = "whoosh"
        confirm: bool = False
        new_stills: bool = False
        seed: int | None = None
        quality: bool = False          # 40-step views instead of Lightning 4-step
        variants: int = 1              # orbits with different seeds, in parallel
        color_match: float = 0.0       # 0..1: match the orbit's colours to the real frame
        guide: float = workflows.GUIDE_STRENGTH
        fix_views: bool = True         # re-roll rejected views before the orbit
        upscale: bool = False          # SeedVR2 pass on the chosen orbit

    @app.post("/api/projects/{pid}/generate")
    def generate(pid: str, body: Generate, x_comfy_key: str | None = Header(None)):
        p = load(pid)
        if body.arc not in arcs.ARCS or body.sound not in ("whoosh", "silent"):
            raise HTTPException(400, "unknown arc or sound")
        n = max(1, min(body.variants, 6))
        est = pipeline.estimate(p, need_stills=True if body.new_stills else None, quality=body.quality,
                                n_variants=n, upscale=body.upscale)
        if not body.confirm:
            return {"confirm_needed": True, **est}
        key = need_key(x_comfy_key)
        subject = body.subject.strip()[:120]
        cm = min(max(body.color_match, 0.0), 1.0)
        guide = min(max(body.guide, 0.2), 1.0)

        def status(step):
            return lambda rec: set_job(p, step=step, comfy=rec.get("status"), prompt_id=rec.get("prompt_id"))

        def vstatus(i, rec):
            set_job(p, step="orbit", comfy=f"variant {i + 1}/{n} {rec.get('status')}", prompt_id=rec.get("prompt_id"))

        def run():
            if "stills" in est["jobs"] or "stills_quality" in est["jobs"]:
                set_job(p, step="stills")
                pipeline.stills(p, key=key, seed=body.seed, on_status=status("stills"), quality=body.quality)
            if body.fix_views and any(not r["ok"] for r in p.state["stills"]["views"].values()):
                set_job(p, step="stills", comfy="re-rolling rejected views")
                pipeline.retry_views(p, key=key, seed=body.seed, on_status=status("stills"))
            if not arcs.available(body.arc, pipeline.usable_views(p)):
                raise ValueError(f"the {body.arc} end view came back unusable; re-roll the stills or pick another arc")
            set_job(p, step="orbit")
            if n > 1:
                pipeline.variants(p, body.arc, n, key=key, seed=body.seed, subject=subject, sound=body.sound,
                                  on_status=vstatus, guide=guide, color_match=cm)
            else:
                pipeline.orbit(p, body.arc, key=key, seed=body.seed, subject=subject, sound=body.sound,
                               on_status=status("orbit"), guide=guide, color_match=cm)
            if body.upscale:
                set_job(p, step="upscale")
                pipeline.upscale(p, key=key, on_status=status("upscale"))
            set_job(p, step="render")
            pipeline.render(p)
        background(p, "generate", run)
        return public(p)

    class Views(BaseModel):
        views: list[str] | None = None
        confirm: bool = False

    @app.post("/api/projects/{pid}/views/retry")
    def retry_views(pid: str, body: Views, x_comfy_key: str | None = Header(None)):
        p = load(pid)
        if not p.state.get("stills"):
            raise HTTPException(400, "generate first")
        todo = body.views or [v for v, r in p.state["stills"]["views"].items() if not r["ok"]]
        if not todo:
            raise HTTPException(400, "every view passed; nothing to re-roll")
        est = {"jobs": ["view"] * len(todo), "usd": round(comfy.job_cost("view") * len(todo), 3)}
        if not body.confirm:
            return {"confirm_needed": True, **est}
        key = need_key(x_comfy_key)
        background(p, "views", lambda: pipeline.retry_views(
            p, todo, key=key, on_status=lambda rec: set_job(p, step="stills", comfy=rec.get("status"))))
        return public(p)

    class Select(BaseModel):
        seed: int
        arc: str | None = None

    @app.post("/api/projects/{pid}/select")
    def select(pid: str, body: Select):
        p = load(pid)
        try:
            pipeline.select_orbit(p, body.seed, body.arc)
        except ValueError as e:
            raise HTTPException(404, str(e))
        background(p, "render", lambda: pipeline.render(p))
        return public(p)

    class Upscale(BaseModel):
        short_side: int = 1080
        confirm: bool = False

    @app.post("/api/projects/{pid}/upscale")
    def upscale(pid: str, body: Upscale, x_comfy_key: str | None = Header(None)):
        p = load(pid)
        if not p.state.get("orbit"):
            raise HTTPException(400, "generate first")
        if not body.confirm:
            return {"confirm_needed": True, "jobs": ["upscale"], "usd": comfy.job_cost("upscale")}
        key = need_key(x_comfy_key)
        side = min(max(body.short_side, 720), 2160) // 2 * 2

        def run():
            set_job(p, step="upscale")
            pipeline.upscale(p, key=key, short_side=side,
                             on_status=lambda rec: set_job(p, step="upscale", comfy=rec.get("status")))
            set_job(p, step="render")
            pipeline.render(p)
        background(p, "upscale", run)
        return public(p)

    @app.get("/api/projects/{pid}/export.zip")
    def export_zip(pid: str, master: bool = True):
        p = load(pid)
        if not p.state.get("render"):
            raise HTTPException(400, "render first")
        try:
            z = pipeline.export(p, master=master)
        except SystemExit as e:
            raise HTTPException(500, f"export failed: {str(e)[:300]}")
        return FileResponse(z, media_type="application/zip", filename=z.name)

    class Render(BaseModel):
        ramp: float = 0.4
        slow: float = 3.0
        sound: str | None = None

    @app.post("/api/projects/{pid}/render")
    def rerender(pid: str, body: Render):
        p = load(pid)
        if not p.state.get("orbit"):
            raise HTTPException(400, "generate first")
        ramp, slow = min(max(body.ramp, 0.0), 1.5), min(max(body.slow, 1.0), 8.0)
        background(p, "render", lambda: pipeline.render(p, ramp_s=ramp, ramp_slow=slow, sound=body.sound))
        return public(p)

    @app.get("/api/projects/{pid}/files/{path:path}")
    def files(pid: str, path: str):
        p = load(pid)
        f = (p.dir / path).resolve()
        if p.dir.resolve() not in f.parents or not f.is_file() or f.name == "state.json":
            raise HTTPException(404)
        return FileResponse(f)

    @app.get("/api/projects/{pid}/workflows.zip")
    def workflows_zip(pid: str):
        p = load(pid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i, pid_ in enumerate(dict.fromkeys(p.state.get("prompt_ids", [])), 1):
                src = comfy.archived_workflow(pid_)
                if src:
                    wf = json.loads(src.read_text())
                    z.writestr(f"{i:02d}_{comfy.workflow_kind(wf)}_{pid_}.json", json.dumps(wf, indent=1))
            z.writestr("README.txt", "API-format ComfyUI graphs exactly as Bullet Time submitted them to Comfy Cloud.\n"
                                     "Queue one with POST /api/prompt, or load it in ComfyUI.\n")
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="bullettime_{pid}_workflows.zip"'})

    @app.get("/api/workflow/{arc}")
    def workflow(arc: str):
        if arc not in arcs.ARCS:
            raise HTTPException(404)
        wf, _ = workflows.build_orbit(arc)
        wf["5"]["inputs"]["text"] = workflows.orbit_prompt(arc)
        return wf

    return app
