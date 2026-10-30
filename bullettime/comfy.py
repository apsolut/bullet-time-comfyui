"""Comfy Cloud client (documented v1 HTTP API) used by the CLI and the web app.
(From one-shot-multicam, same author, MIT: https://github.com/apsolut/one-shot-multicam)

Docs: https://docs.comfy.org/development/cloud/overview
      https://docs.comfy.org/development/cloud/api-reference
Flow: POST /api/upload/image (image AND audio inputs; the multipart field is named "image")
      -> POST /api/prompt {"prompt": <API-format workflow>}
      -> GET /api/job/{id}/status until terminal -> GET /api/jobs/{id} (outputs)
      -> GET /api/view (302 -> signed storage URL, fetched WITHOUT the API key).
Auth: header `X-API-Key`. Every function takes an explicit `key`; the CLI falls back to
$COMFY_API_KEY (or $COMFY_CLOUD_API_KEY). The key is never printed, logged or written to disk.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

BASE_URL = os.environ.get("COMFY_CLOUD_BASE_URL", "https://cloud.comfy.org")
TERMINAL = {"success", "error", "non_retryable_error", "lost", "cancelled"}
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
# Measured on Comfy Cloud, Oct 2026 (balance delta / jobs). stills = 8 Qwen views (Lightning 4 steps);
# stills_quality = the same at 40 steps cfg 4 (estimate); view = one re-rolled view (model load dominates: $0.025 measured);
# upscale = SeedVR2 3B on a 193-frame orbit.
JOB_COST_USD = {"stills": 0.045, "stills_quality": 0.25, "view": 0.025, "orbit": 0.067, "orbit_ltx25": 0.067,
                "orbit_ltx23": 0.055, "upscale": 0.12}
COST_PER_JOB_USD = max(JOB_COST_USD.values())  # conservative figure for budget guards

# Studio guard rails: at most this many Comfy jobs in flight per process (BULLETTIME_PARALLEL_JOBS), and transient
# failures (network, 5xx, 429 rate limit) are retried with backoff instead of failing a paid pipeline.
MAX_JOBS = max(1, int(os.environ.get("BULLETTIME_PARALLEL_JOBS", "3")))
RETRIES = 4
_slots = threading.BoundedSemaphore(MAX_JOBS)


def job_cost(kind: str) -> float:
    return JOB_COST_USD.get(kind, COST_PER_JOB_USD)


class ComfyError(RuntimeError):
    pass


def env_key() -> str | None:
    return os.environ.get("COMFY_API_KEY") or os.environ.get("COMFY_CLOUD_API_KEY")


def _key(key: str | None) -> str:
    key = key or env_key()
    if not key:
        raise ComfyError("no Comfy Cloud API key (set COMFY_API_KEY; create one at https://platform.comfy.org/profile/api-keys)")
    return key


def _headers(key, json_body=True):
    h = {"X-API-Key": _key(key)}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _err(r) -> str:
    return f"HTTP {r.status_code} {r.request.method} {r.url.split('?')[0]}: {r.text[:1500]}"


def _request(method: str, url: str, retries: int = RETRIES, **kw):
    """requests.request with retries on connection errors, timeouts, 5xx and 429 (backoff 2, 4, 8, 16 s).
    4xx other than 429 are returned as they are (the caller reports them)."""
    import requests
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code < 500 and r.status_code != 429:
                return r
            if r.status_code == 429 and "subscription" in r.text.lower():
                return r
            last = ComfyError(_err(r))
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        if attempt < retries:
            time.sleep(2 ** (attempt + 1))
    raise ComfyError(f"Comfy Cloud unreachable after {retries + 1} tries: {last}")


def check_key(key=None) -> dict:
    r = _request("GET", f"{BASE_URL}/api/user", headers=_headers(key), timeout=30)
    if r.status_code in (401, 403):
        raise ComfyError("Comfy Cloud rejected the API key (401)")
    if r.status_code == 429:
        raise ComfyError("Comfy Cloud: subscription inactive (429). API access needs a paid plan")
    if not r.ok:
        raise ComfyError(_err(r))
    return r.json()


def balance_usd(key=None) -> float | None:
    try:
        r = _request("GET", f"{BASE_URL}/api/billing/usage/timeseries", headers=_headers(key), timeout=30, retries=1,
                     params={"granularity": "day", "months": 1})
        return round(r.json()["summary"]["balance"]["amount_cents"] / 100.0, 4) if r.ok else None
    except Exception:  # noqa: BLE001
        return None


def upload(path: str, key=None) -> str:
    """Upload an input file; returns the name to put into LoadImage/LoadAudio."""
    with open(path, "rb") as f:
        r = _request("POST", f"{BASE_URL}/api/upload/image", headers=_headers(key, False), timeout=300, retries=2,
                     files={"image": (Path(path).name, f)}, data={"type": "input", "overwrite": "true"})
    if not r.ok:
        raise ComfyError(f"upload failed for {Path(path).name}: {_err(r)}")
    body = r.json()
    return f"{body['subfolder']}/{body['name']}" if body.get("subfolder") else body["name"]


def _pairs(b):
    return [b] if b and isinstance(b[0], str) else b


def load_bindings(path) -> dict:
    return {k: v for k, v in json.loads(Path(path).read_text()).items() if not k.startswith("_")}


def patch_workflow(workflow: dict, bindings: dict, values: dict) -> dict:
    wf = copy.deepcopy(workflow)
    for key, val in values.items():
        if key not in bindings or val is None:
            continue
        for node, inp in _pairs(bindings[key]):
            if node not in wf:
                raise ComfyError(f"binding {key}: node {node} not in workflow")
            if inp not in wf[node]["inputs"]:
                raise ComfyError(f"binding {key}: node {node} has no input {inp}")
            wf[node]["inputs"][inp] = val
    return wf


# Workflow archive: every graph submitted to Comfy Cloud is also written to <dir>/<prompt_id>.json (the API
# graph only; keys travel in headers, never in it), so the web app can offer "Download ComfyUI workflow" for
# each finished job. Process-wide (works from any worker thread); the server sets it to <data>/workflows.
_ARCHIVE: dict = {"dir": None, "keep_s": 2 * 86400}


def set_workflow_archive(dirpath, keep_s: float = 2 * 86400):
    _ARCHIVE["dir"] = Path(dirpath) if dirpath else None
    _ARCHIVE["keep_s"] = keep_s


def archived_workflow(prompt_id: str) -> Path | None:
    d = _ARCHIVE["dir"]
    if not d or not prompt_id or not all(c.isalnum() or c == "-" for c in prompt_id):
        return None
    f = d / f"{prompt_id}.json"
    return f if f.exists() else None


def workflow_kind(workflow: dict) -> str:
    """Short label for a graph (from its models), used in exported file names."""
    blob = json.dumps(workflow).lower()
    for needle, name in (("bullettime", "bullettime-orbit"), ("multiple-angles", "orbit-stills"),
                         ("qwen", "qwen-edit"), ("ltx", "ltx")):
        if needle in blob:
            return name
    return "workflow"


def export_workflows(prompt_ids, dest) -> list[Path]:
    """Copy the archived graphs of these jobs into dest as <nn>_<kind>_<prompt_id>.json (idempotent)."""
    dest = Path(dest)
    out = []
    for n, pid in enumerate([p for p in dict.fromkeys(prompt_ids or []) if p], 1):
        src = archived_workflow(pid)
        if not src:
            continue
        existing = list(dest.glob(f"*_{pid}.json")) if dest.exists() else []
        if existing:
            out.append(existing[0]); continue
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / f"{n:02d}_{workflow_kind(json.loads(src.read_text()))}_{pid}.json"
        f.write_text(src.read_text())
        out.append(f)
    return out


def _save_submitted(workflow: dict, prompt_id: str):
    d = _ARCHIVE["dir"]
    if not d:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{prompt_id}.json").write_text(json.dumps(workflow, indent=1))
        cutoff = time.time() - _ARCHIVE["keep_s"]
        for f in d.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass  # never fail a paid job because the copy couldn't be written


def submit(workflow: dict, key=None) -> str:
    r = _request("POST", f"{BASE_URL}/api/prompt", headers=_headers(key), json={"prompt": workflow}, timeout=120)
    if not r.ok:
        raise ComfyError(f"workflow rejected: {_err(r)}")
    body = r.json()
    if body.get("node_errors"):
        raise ComfyError(f"workflow rejected (node_errors): {json.dumps(body['node_errors'])[:1500]}")
    _save_submitted(workflow, body["prompt_id"])  # the graph only: keys travel in headers, never in it
    return body["prompt_id"]


def wait(prompt_id: str, key=None, timeout: float = 1800, poll: float = 5.0,
         on_status: Callable[[str], None] | None = None) -> str:
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        r = _request("GET", f"{BASE_URL}/api/job/{prompt_id}/status", headers=_headers(key), timeout=60)
        if r.ok:
            st = r.json().get("status")
            if st != last:
                last = st
                if on_status:
                    on_status(st)
                else:
                    print(f"  [{time.time() - t0:6.0f}s] {prompt_id}: {st}", flush=True)
            if st in TERMINAL:
                return st
        time.sleep(poll)
    raise TimeoutError(f"{prompt_id} not finished after {timeout}s")


def job_detail(prompt_id: str, key=None) -> dict:
    r = _request("GET", f"{BASE_URL}/api/jobs/{prompt_id}", headers=_headers(key), timeout=60)
    if not r.ok:
        raise ComfyError(f"job detail failed: {_err(r)}")
    return r.json()


def find_files(outputs: dict, exts) -> list[dict]:
    found = []
    for node_id, node_out in (outputs or {}).items():
        if not isinstance(node_out, dict):
            continue
        for items in node_out.values():
            if isinstance(items, list):
                for fi in items:
                    if isinstance(fi, dict) and str(fi.get("filename", "")).lower().endswith(exts):
                        found.append({**fi, "node": node_id})
    return found


def find_videos(outputs: dict) -> list[dict]:
    return find_files(outputs, VIDEO_EXT)


def download(fi: dict, out_path: Path, key=None) -> Path:
    """GET /api/view returns a 302 to a signed URL; follow it without forwarding the API key."""
    params = {"filename": fi["filename"], "subfolder": fi.get("subfolder", ""), "type": fi.get("type", "output")}
    r = _request("GET", f"{BASE_URL}/api/view", headers=_headers(key, False), params=params, allow_redirects=False,
                 timeout=120)
    if r.status_code in (301, 302, 303, 307, 308):
        r = _request("GET", r.headers["Location"], timeout=600)
    if not r.ok:
        raise ComfyError(f"download failed: {_err(r)}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(r.content)
    return out_path


def is_file(v) -> bool:
    """True for a string naming an existing file (a long prompt string is not a path, and must not raise)."""
    if not isinstance(v, str) or len(v) > 1024 or "\n" in v:
        return False
    try:
        return Path(v).is_file()
    except OSError:
        return False


def run_workflow(workflow: dict, bindings: dict, values: dict, out_dir: Path, key=None,
                 on_status: Callable[[dict], None] | None = None, rename: dict | None = None) -> dict:
    """Run any API-format workflow once: file-path values are uploaded; every output file is downloaded.
    At most MAX_JOBS run at once per process; the rest wait their turn (status "waiting")."""
    values = dict(values)
    rec = {"status": "waiting", "started": time.time(), "outputs": []}
    emit = (lambda: on_status(dict(rec))) if on_status else (lambda: None)
    emit()
    with _slots:
        return _run_workflow(workflow, bindings, values, out_dir, key, rec, emit, rename)


def _run_workflow(workflow, bindings, values, out_dir, key, rec, emit, rename) -> dict:
    rec["status"] = "uploading"; emit()
    for k, v in list(values.items()):
        if is_file(v):
            values[k] = upload(v, key)
    rec["prompt_id"] = submit(patch_workflow(workflow, bindings, values), key)
    rec["status"] = "submitted"; emit()

    def st_cb(st):
        rec["status"] = st; emit()
    st = wait(rec["prompt_id"], key, on_status=st_cb)
    rec["elapsed_s"] = round(time.time() - rec["started"], 1)
    if st == "success":
        out = Path(out_dir)
        for fi in find_files(job_detail(rec["prompt_id"], key).get("outputs"), IMAGE_EXT + VIDEO_EXT):
            if fi.get("type", "output") != "output":
                continue
            name = (rename or {}).get(fi["node"])
            dest = out / (f"{name}{Path(fi['filename']).suffix}" if name else
                          f"{fi['node'].replace(':', '_')}_{Path(fi['filename']).name}")
            rec["outputs"].append(str(download(fi, dest, key)))
    else:
        rec["error"] = json.dumps(job_detail(rec["prompt_id"], key).get("execution_error"))[:1500]
    emit()
    return rec


def job_status(prompt_id: str, key=None) -> str | None:
    """Current status of an already-submitted job (no new submission, free). None if unknown."""
    r = _request("GET", f"{BASE_URL}/api/job/{prompt_id}/status", headers=_headers(key), timeout=60, retries=1)
    return r.json().get("status") if r.ok else None


def fetch_finished(prompt_id: str, out_path: Path, key=None) -> str | None:
    """Resume after a server restart: if the Comfy job already finished, download its first video into
    out_path. Never submits anything. -> the job status ("success" = downloaded), or None if unknown."""
    st = job_status(prompt_id, key)
    if st == "success":
        vids = find_videos(job_detail(prompt_id, key).get("outputs"))
        if not vids:
            return "error"
        download(vids[0], out_path, key)
    return st
