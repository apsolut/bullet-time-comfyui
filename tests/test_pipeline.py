"""Offline end-to-end tests: Comfy Cloud is faked, ffmpeg and OpenCV run for real."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from bullettime import arcs, assemble, comfy, pick, pipeline, workflows
from bullettime.util import probe

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


@pytest.fixture(scope="session")
def take(tmp_path_factory) -> Path:
    """A 4 s portrait take: a box that flies fast between 1.8 s and 2.2 s, with a tone."""
    out = tmp_path_factory.mktemp("take") / "take.mp4"
    box = "drawbox=x='if(between(t,1.8,2.2),60+(t-1.8)*900,60)':y=500:w=200:h=200:color=white:t=fill"
    ff("-f", "lavfi", "-i", "color=c=0x334455:s=720x1280:r=30:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4",
       "-vf", box, "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", out)
    return out


def fake_run_workflow(workflow, bindings, values, out_dir, key=None, on_status=None, rename=None):
    """Stands in for Comfy Cloud: stills -> 8 portrait PNGs, orbit -> a clip of the arc's length."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    patched = comfy.patch_workflow(workflow, bindings, {k: (Path(v).name if comfy.is_file(v) else v)
                                                        for k, v in values.items()})
    if on_status:
        on_status({"status": "submitted", "prompt_id": "fake"})
    files = []
    if rename:  # the stills job
        for node, view in rename.items():
            f = out / f"{view}.png"
            ff("-f", "lavfi", "-i", "color=c=0x556677:s=768x1344", "-frames:v", "1", f)
            files.append(str(f))
    else:
        n = patched["10"]["inputs"]["length"]
        f = out / "bullettime_orbit_00001.mp4"
        ff("-f", "lavfi", "-i", f"testsrc=s=704x1280:r=24,trim=end_frame={n}", "-f", "lavfi", "-i", f"sine=f=150:d={n / 24}",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", f)
        files.append(str(f))
    return {"status": "success", "prompt_id": f"fake-{time.time_ns()}", "outputs": files}


def test_arcs_start_and_end_on_the_real_frame():
    for name, a in arcs.ARCS.items():
        idx = dict(zip(a["keys"], arcs.key_frame_idx(name)))
        play = a["play"]
        real = a["keys"].index("real")
        assert play[0][0] == real and play[-1][1] == real, name
        for (x0, x1, _), (y0, _, _) in zip(play, play[1:]):
            assert x1 == y0, f"{name}: playback must be continuous"
        assert all(i % 8 == 0 for i in idx.values()), "LTX guides need frame indices divisible by 8"
        assert (arcs.frames(name) - 1) % 8 == 0


def test_orbit_graph_is_closed_and_bound():
    for name in arcs.ARCS:
        wf, b = workflows.build_orbit(name)
        for nid, node in wf.items():
            for v in node["inputs"].values():
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                    assert v[0] in wf, f"{name}: node {nid} links to missing {v[0]}"
        assert set(arcs.ARCS[name]["keys"]) <= set(b)
        filled = comfy.patch_workflow(wf, b, {"prompt": workflows.orbit_prompt(name), "seed": 7})
        assert "frozen" in filled["5"]["inputs"]["text"]
        guides = sorted(n["inputs"]["frame_idx"] for n in wf.values() if n["class_type"] == "LTXVAddGuide")
        assert guides, name
        assert not any(c in json.dumps(wf) for c in ("partner", "Api", "api_key")), "open weights only"


def test_dropped_keys_are_not_in_graph():
    wf, b = workflows.build_orbit("sweep", keys=["l", "r"])
    assert "fl" not in b and "fr" not in b
    assert sum(n["class_type"] == "LoadImage" for n in wf.values()) == 3


def test_stills_prompts_use_lora_format():
    wf, b, rename = workflows.stills_workflow()
    vals = workflows.stills_values("x.png", seed=3)
    assert all(vals[f"p{i}"].startswith("<sks> ") for i in range(1, 9))
    assert sorted(rename.values()) == sorted(arcs.VIEWS)
    comfy.patch_workflow(wf, b, vals)


def test_pick_finds_the_action(take):
    res = pick.analyze(take)
    assert 1.7 <= res["t"] <= 2.35, res["t"]


def test_full_pipeline_offline(take, tmp_path, monkeypatch):
    monkeypatch.setattr(comfy, "run_workflow", fake_run_workflow)
    p = pipeline.new_project(take, tmp_path)
    st = pipeline.stills(p, key="k")
    assert set(st["views"]) == set(arcs.VIEWS)
    assert all(v["ok"] for v in st["views"].values())
    o = pipeline.orbit(p, "sweep", key="k")
    assert o["keys"] == ["l", "fl", "real", "fr", "r"]
    rep = pipeline.render(p)
    info = probe(p.path(rep["out"]))
    expect = 4.0 + arcs.freeze_seconds("sweep") + 0.4 * 2  # take + freeze + ramp slow-down (0.4 s at 1/3)
    assert abs(info["duration"] - expect) < 0.15, (info["duration"], expect)
    assert (info["width"], info["height"]) == (720, 1280)
    assert probe(p.path(rep["orbit_only"]))["duration"] > arcs.freeze_seconds("sweep")


def test_render_is_continuous_at_the_cuts(take, tmp_path, monkeypatch):
    """The frame right after the freeze must be the frame at t in the real take."""
    monkeypatch.setattr(comfy, "run_workflow", fake_run_workflow)
    p = pipeline.new_project(take, tmp_path)
    pipeline.stills(p, key="k")
    pipeline.orbit(p, "orbit-right", key="k")
    rep = pipeline.render(p, ramp_s=0)
    segs = rep["segments"]
    resume = sum((s["end"] - s["start"]) / s["speed"] for s in segs[:-1])
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    ff("-ss", f"{resume + 0.02:.3f}", "-i", p.path(rep["out"]), "-frames:v", "1", "-vf", "scale=90:160", a)
    ff("-ss", f"{p.state['t'] + 0.02:.3f}", "-i", take, "-frames:v", "1", "-vf", "scale=90:160", b)
    import numpy as np
    from PIL import Image
    d = np.abs(np.asarray(Image.open(a), float) - np.asarray(Image.open(b), float)).mean()
    assert d < 12, d


def test_assemble_without_audio(tmp_path):
    take = tmp_path / "silent.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", take)
    orb = tmp_path / "orb.mp4"
    ff("-f", "lavfi", "-i", "testsrc=s=704x1280:r=24,trim=end_frame=49", "-c:v", "libx264", "-pix_fmt", "yuv420p", orb)
    rep = assemble.render(take, orb, 1.5, "crane", tmp_path / "o.mp4")
    assert "sample_rate" in probe(tmp_path / "o.mp4")
    assert rep["width"] == 1080


def test_server_flow(take, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from bullettime.server import create_app
    monkeypatch.setattr(comfy, "run_workflow", fake_run_workflow)
    monkeypatch.delenv("COMFY_API_KEY", raising=False)  # the 401 check below needs no key in the env
    monkeypatch.delenv("COMFY_CLOUD_API_KEY", raising=False)
    c = TestClient(create_app(tmp_path))
    with open(take, "rb") as f:
        r = c.post("/api/projects", files={"file": ("take.mp4", f, "video/mp4")})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert c.patch(f"/api/projects/{pid}", json={"t": 2.0}).json()["t"] == 2.0
    est = c.post(f"/api/projects/{pid}/generate", json={"arc": "orbit-left"}).json()
    assert est["confirm_needed"] and est["jobs"] == ["stills", "orbit"]
    assert c.post(f"/api/projects/{pid}/generate", json={"arc": "orbit-left", "confirm": True}).status_code == 401
    r = c.post(f"/api/projects/{pid}/generate", json={"arc": "orbit-left", "confirm": True}, headers={"X-Comfy-Key": "k"})
    assert r.status_code == 200, r.text
    for _ in range(120):
        s = c.get(f"/api/projects/{pid}").json()
        if s["job"]["status"] != "running":
            break
        time.sleep(0.5)
    assert s["job"]["status"] == "done", s["job"]
    assert c.get(f"/api/projects/{pid}/files/{s['render']['out']}").status_code == 200
    assert c.get(f"/api/projects/{pid}/files/state.json").status_code == 404
    assert c.get(f"/api/projects/{pid}/files/../../etc/passwd").status_code == 404
    assert "k" not in (tmp_path / "projects" / pid / "state.json").read_text().split('"')  # key never stored
    assert c.post(f"/api/projects/{pid}/generate", json={"arc": "orbit-left"}).json()["jobs"] == ["orbit"]
