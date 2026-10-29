"""Local web app: `bullettime serve` -> http://127.0.0.1:8790

The Comfy Cloud key lives in the browser (localStorage) and arrives per request in `X-Comfy-Key`. It is
kept in memory for that one action only: never written to disk, logged or returned. (A local server is
needed because Comfy Cloud's API sends no CORS headers, so a page can't call it directly.)
"""

import io
import json
import threading
import time
import traceback
import zipfile
from pathlib import Path

from . import arcs, comfy, pipeline, workflows

WEB = Path(__file__).resolve().parent / "web"
MAX_UPLOAD_MB = 80
MAX_UPLOAD_S = 30.0


def create_app(data_dir: str | Path):
    from fastapi import FastAPI, File, Header, HTTPException, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse, Response
    from pydantic import BaseModel

    data = Path(data_dir)
    projects = data / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    comfy.set_workflow_archive(data / "workflows")
    locks: dict[str, threading.Lock] = {}
    app = FastAPI(title="Bullet Time", docs_url="/api/docs", openapi_url="/api/openapi.json")

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
        return s

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

    class Generate(BaseModel):
        arc: str = arcs.DEFAULT_ARC
        subject: str = ""
        sound: str = "whoosh"
        confirm: bool = False
        new_stills: bool = False
        seed: int | None = None

    @app.post("/api/projects/{pid}/generate")
    def generate(pid: str, body: Generate, x_comfy_key: str | None = Header(None)):
        p = load(pid)
        if body.arc not in arcs.ARCS or body.sound not in ("whoosh", "silent"):
            raise HTTPException(400, "unknown arc or sound")
        est = pipeline.estimate(p, need_stills=True if body.new_stills else None)
        if not body.confirm:
            return {"confirm_needed": True, **est}
        key = x_comfy_key or comfy.env_key()
        if not key:
            raise HTTPException(401, "paste your Comfy Cloud key first")
        subject = body.subject.strip()[:120]

        def status(step):
            return lambda rec: set_job(p, step=step, comfy=rec.get("status"), prompt_id=rec.get("prompt_id"))

        def run():
            if "stills" in est["jobs"]:
                set_job(p, step="stills")
                pipeline.stills(p, key=key, seed=body.seed, on_status=status("stills"))
                p.state = pipeline.Project(p.dir).state
            if not arcs.available(body.arc, pipeline.usable_views(p)):
                raise ValueError(f"the {body.arc} end view came back unusable; re-roll the stills or pick another arc")
            set_job(p, step="orbit")
            pipeline.orbit(p, body.arc, key=key, seed=body.seed, subject=subject, sound=body.sound,
                           on_status=status("orbit"))
            p.state = pipeline.Project(p.dir).state
            set_job(p, step="render")
            pipeline.render(p)
        background(p, "generate", run)
        return public(p)

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
