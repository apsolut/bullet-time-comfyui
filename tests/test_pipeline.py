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
    elif "SeedVR2VideoUpscaler" in json.dumps(patched):  # the upscale job: same clip at 2x
        src = next(v for v in values.values() if comfy.is_file(v))
        f = out / "bullettime_orbit_up_00001.mp4"
        ff("-i", src, "-vf", "scale=iw*2:ih*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", f)
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
        end = a["keys"].index("real_end") if "real_end" in a["keys"] else real
        assert play[0][0] == real and play[-1][1] == end, name
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
        assert set(arcs.ARCS[name]["keys"]) - {"real_end"} <= set(b)
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
    for v in ("COMFY_API_KEY", "COMFY_CLOUD_API_KEY", "BULLETTIME_PASSWORD", "BULLETTIME_HOSTED_RUNS",
              "BULLETTIME_JUDGE_CODE", "BULLETTIME_PROMO_CODES", "BULLETTIME_PUBLIC"):
        monkeypatch.delenv(v, raising=False)  # a plain bring-your-own-key server: the 403 check below needs no key
    c = TestClient(create_app(tmp_path))
    with open(take, "rb") as f:
        r = c.post("/api/projects", files={"file": ("take.mp4", f, "video/mp4")})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert c.patch(f"/api/projects/{pid}", json={"t": 2.0}).json()["t"] == 2.0
    est = c.post(f"/api/projects/{pid}/generate", json={"arc": "orbit-left"}).json()
    assert est["confirm_needed"] and est["jobs"] == ["stills", "orbit"]
    assert c.post(f"/api/projects/{pid}/generate", json={"arc": "orbit-left", "confirm": True}).status_code == 403
    assert c.get("/api/config").json()["byok_only"] is True
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


@pytest.fixture(autouse=True)
def no_billing(monkeypatch):
    monkeypatch.setattr(comfy, "balance_usd", lambda key=None: 10.0)


def test_single_view_graph_is_a_pruned_template():
    full, fb, _ = workflows.stills_workflow()
    one, b, rename = workflows.stills_workflow(views=["r"])
    assert list(rename.values()) == ["r"] and len(one) < len(full) / 4
    for nid, node in one.items():
        for v in node["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                assert v[0] in one, f"node {nid} links to pruned {v[0]}"
    assert set(b) == {"image", "angle_lora", "img2", "l2", "p2", "s2"}
    vals = workflows.stills_values("x.png", 1, "close-up", views=["r"])
    assert set(vals) == {"image", "angle_lora", "p2", "s2"} and vals["p2"].startswith("<sks> right side view")
    q, _, _ = workflows.stills_workflow(quality=True)
    assert all(n["inputs"]["value"] is False for n in q.values() if n["class_type"] == "PrimitiveBoolean")


def test_color_match_and_upscale_graphs():
    w, _ = workflows.build_orbit("sweep", color_match=0.5)
    cm = [n for n in w.values() if n["class_type"] == "ColorMatch"]
    assert len(cm) == 1 and cm[0]["inputs"]["strength"] == 0.5
    assert w["62"]["inputs"]["images"][0] == "65"  # the video is made from the matched frames
    w0, _ = workflows.build_orbit("sweep")
    assert not [n for n in w0.values() if n["class_type"] == "ColorMatch"]
    u, b = workflows.build_upscale(short_side=1440)
    assert u["5"]["inputs"]["resolution"] == 1440 and b["video"] == [["1", "file"]]


def test_variants_retry_upscale_export(take, tmp_path, monkeypatch):
    monkeypatch.setattr(comfy, "run_workflow", fake_run_workflow)
    p = pipeline.new_project(take, tmp_path)
    pipeline.stills(p, key="k")
    # pretend one view failed its check, then re-roll just that one
    p.state["stills"]["views"]["r"].update(ok=False, reason="test"); p.save()
    calls = []
    real_fake = fake_run_workflow

    def spy(workflow, bindings, values, out_dir, key=None, on_status=None, rename=None):
        calls.append((len(workflow), sorted(rename or {})))
        return real_fake(workflow, bindings, values, out_dir, key, on_status, rename)
    monkeypatch.setattr(comfy, "run_workflow", spy)
    st = pipeline.retry_views(p, key="k")
    assert st["views"]["r"]["ok"] and calls[-1][0] < 40  # one branch, not the whole template
    outs = pipeline.variants(p, "orbit-right", 3, key="k", seed=5)
    assert len(outs) == 3 and len({o["seed"] for o in outs}) == 3 and len(p.state["orbits"]) == 3
    assert p.state["orbit"]["seed"] == outs[0]["seed"]
    pipeline.select_orbit(p, outs[2]["seed"])
    assert p.state["orbit"]["seed"] == outs[2]["seed"] and "render" not in p.state
    pipeline.upscale(p, key="k")
    assert p.state["orbit"]["path_up"].endswith("orbit_up.mp4")
    assert probe(p.path(p.state["orbit"]["path_up"]))["width"] == 1408
    rep = pipeline.render(p)
    assert rep["orbit_clip"].endswith("orbit_up.mp4")
    kinds = [e["kind"] for e in p.state["ledger"]]
    assert kinds == ["stills", "view", "orbit", "orbit", "orbit", "upscale"]
    z = pipeline.export(p)
    import zipfile
    names = zipfile.ZipFile(z).namelist()
    assert {"bullettime.mp4", "bullettime_master.mov", "timeline.json", "frozen_frame.png", "views/r.png"} <= set(names)
    assert any(n.startswith("orbit/") for n in names)
    assert probe(tmp_path / "x" if False else z.parent / "bullettime_master.mov")["codec_name" if False else "width"] == 720


def test_closed_eyes_are_never_frozen():
    from bullettime import eyes
    blink = [0.1] * 10 + [0.8, 0.9, 0.7] + [0.1] * 10  # a blink at frames 10-12
    closed = eyes.closed_frames(blink)
    assert {8, 9, 10, 11, 12, 13, 14} <= closed and 7 not in closed and 15 not in closed
    assert eyes.nearest_open(blink, 11) == 15  # later first: the eyes reopen forwards
    assert eyes.nearest_open(blink, 3) == 3
    assert eyes.nearest_open(None, 11) == 11  # no detector: unchanged


# ---------- hosted mode (free runs, promo codes, own key), ported from one-shot-multicam
from bullettime.hosted import Hosted  # noqa: E402

LEGACY, P1, P2, P3 = "judge-code-for-tests", "BT-AAAA-1111", "BT-BBBB-2222", "BT-CCCC-3333"


def mk(tmp_path, **env):
    base = {"COMFY_API_KEY": "op-key-not-real", "BULLETTIME_JUDGE_CODE": LEGACY,
            "BULLETTIME_PROMO_CODES": f"{P1}:10, {P2}:3 ,{P3}", "BULLETTIME_HOSTED_RUNS": "6",
            "BULLETTIME_TRIAL_JOBS_PER_RUN": "4", "BULLETTIME_JUDGE_MAX_JOBS": "7"}
    base.update(env)
    return Hosted(tmp_path / "d", env=base)


def test_promo_codes_parse_match_and_count(tmp_path):
    H = mk(tmp_path)
    codes = H.promo_codes()
    assert codes["judge"] == (LEGACY, 7) and codes[H.promo_id(P1)] == (P1, 10) and codes[H.promo_id(P3)] == (P3, 7)
    assert H.resolve_key(None, LEGACY) == ("op-key-not-real", "promo:judge")
    assert H.resolve_key(None, f"  {P1} ")[1] == "promo:" + H.promo_id(P1)
    with pytest.raises(PermissionError):
        H.resolve_key(None, "BT-ZZZZ-9999")
    H.charge_hosted(2, "1.1.1.1", promo=H.promo_id(P1))
    H.charge_hosted(1, "1.1.1.1", promo="judge")
    assert H.promo_used(H.promo_id(P1)) == 2 and H.promo_used(H.promo_id(P2)) == 0 and H.promo_used("judge") == 1
    H.refund_hosted(1, "1.1.1.1", promo=H.promo_id(P1))
    assert H.promo_used(H.promo_id(P1)) == 1
    raw = H.counter_file.read_text()
    assert P1 not in raw and LEGACY not in raw and H.promo_id(P1) in raw  # only hashes on disk
    H.charge_hosted(3, "2.2.2.2", promo=H.promo_id(P2))
    with pytest.raises(PermissionError, match="used up"):
        H.charge_hosted(1, "2.2.2.2", promo=H.promo_id(P2))
    H2 = mk(tmp_path, BULLETTIME_PROMO_CODES=f"{P2}:3")  # P1 removed from the env = revoked
    assert H2.promo_match(P1) is None and H2.promo_match(P2) and H2.promo_match(LEGACY) == "judge"
    with pytest.raises(PermissionError):
        H2.charge_hosted(1, "3.3.3.3", promo=H.promo_id(P1))


def test_pay_mode_is_explicit_and_trial_is_one_per_visitor(tmp_path):
    H = mk(tmp_path)
    assert H.resolve_key("mine", LEGACY) == ("mine", "key")            # no mode: own key wins
    assert H.resolve_key("mine", LEGACY, "promo")[1] == "promo:judge"   # explicit mode wins over the key
    assert H.resolve_key("mine", None, "trial") == ("op-key-not-real", "trial")
    with pytest.raises(PermissionError):
        H.resolve_key(None, None, "key")  # chose own key, sent none: never falls back to the operator
    assert H.resolve_key(None, None) == ("op-key-not-real", "trial")
    H.charge("trial", 2, "9.9.9.9", "cookie-a", "proj1")
    H.charge("trial", 2, "9.9.9.9", "cookie-a", "proj1")  # same project shares the run (4 jobs max)
    with pytest.raises(PermissionError, match="free run covers"):
        H.charge("trial", 1, "9.9.9.9", "cookie-a", "proj1")
    with pytest.raises(PermissionError, match="used your free run"):
        H.charge("trial", 1, "9.9.9.9", "cookie-b", "proj2")  # same IP, new cookie
    with pytest.raises(PermissionError, match="used your free run"):
        H.charge("trial", 1, "8.8.8.8", "cookie-a", "proj3")  # new IP, same cookie
    st = H.trial_status("7.7.7.7", "cookie-c")
    assert st == {"runs_total": 6, "runs_left": 5, "used_by_you": False, "jobs_per_run": 4}
    H.refund("trial", 4, "9.9.9.9", "proj1")  # nothing was submitted: the run is released
    assert H.trial_status("9.9.9.9", "cookie-a")["runs_left"] == 6
    none = mk(tmp_path, COMFY_API_KEY="")
    assert not none.any_free and none.config("1.1.1.1", None)["byok_only"]
    with pytest.raises(PermissionError, match="key required"):
        none.resolve_key(None, None)


def test_server_hosted_flow(take, tmp_path, monkeypatch):
    """Public site: a visitor without a key pays with a free run; a promo code pays on its own counter;
    projects are never listed; reservations are refunded for jobs that never ran."""
    from fastapi.testclient import TestClient
    from bullettime.server import create_app
    monkeypatch.setattr(comfy, "run_workflow", fake_run_workflow)
    monkeypatch.setattr(comfy, "check_key", lambda key=None: {"ok": True})
    for v, val in {"COMFY_API_KEY": "op-key", "BULLETTIME_PASSWORD": "", "BULLETTIME_PUBLIC": "1",
                   "BULLETTIME_HOSTED_RUNS": "2", "BULLETTIME_TRIAL_JOBS_PER_RUN": "3",
                   "BULLETTIME_JUDGE_CODE": "judge-code-for-tests", "BULLETTIME_PROMO_CODES": ""}.items():
        monkeypatch.setenv(v, val) if val else monkeypatch.delenv(v, raising=False)
    c = TestClient(create_app(tmp_path))
    cfg = c.get("/api/config").json()
    assert cfg["trial"]["runs_left"] == 2 and cfg["judge_code"] and not cfg["byok_only"] and cfg["public"]
    assert c.post("/api/key/check").json()["trial"]["runs_left"] == 2
    assert c.post("/api/key/check", headers={"X-Judge-Code": "judge-code-for-tests"}).json()["judge_jobs_left"] == 10
    assert c.post("/api/key/check", headers={"X-Judge-Code": "nope-nope"}).status_code == 403
    with open(take, "rb") as f:
        pid = c.post("/api/projects", files={"file": ("take.mp4", f, "video/mp4")}).json()["id"]
    assert c.get("/api/projects").json() == []  # public: no listing
    # a free run: stills + orbit = 2 jobs (+1 reserved for a possible view re-roll, refunded when unused)
    r = c.post(f"/api/projects/{pid}/generate", json={"arc": "orbit-left", "confirm": True}, headers={"X-Pay-Mode": "trial"})
    assert r.status_code == 200, r.text
    for _ in range(120):
        s = c.get(f"/api/projects/{pid}").json()
        if s["job"]["status"] != "running":
            break
        time.sleep(0.5)
    assert s["job"]["status"] == "done", s["job"]
    t = c.get("/api/config").json()["trial"]
    assert t["runs_left"] == 1 and t["used_by_you"]
    ledger = json.loads((tmp_path / "hosted_jobs.json").read_text())
    assert list(ledger["trial"]["jobs"].values()) == [2]  # 3 reserved, 1 refunded
    # the same visitor can't start a second free run on another project; a promo code can
    with open(take, "rb") as f:
        pid2 = c.post("/api/projects", files={"file": ("take.mp4", f, "video/mp4")}).json()["id"]
    r = c.post(f"/api/projects/{pid2}/generate", json={"arc": "orbit-left", "confirm": True}, headers={"X-Pay-Mode": "trial"})
    assert r.status_code == 403 and "free run" in r.json()["detail"]
    r = c.post(f"/api/projects/{pid2}/generate", json={"arc": "orbit-left", "confirm": True},
               headers={"X-Pay-Mode": "promo", "X-Judge-Code": "judge-code-for-tests"})
    assert r.status_code == 200, r.text
    for _ in range(120):
        s = c.get(f"/api/projects/{pid2}").json()
        if s["job"]["status"] != "running":
            break
        time.sleep(0.5)
    assert s["job"]["status"] == "done"
    assert c.post("/api/key/check", headers={"X-Judge-Code": "judge-code-for-tests"}).json()["judge_jobs_left"] == 8
    # explicit "key" mode with no key never falls back to the operator's credits
    assert c.post(f"/api/projects/{pid2}/generate", json={"arc": "sweep", "confirm": True}, headers={"X-Pay-Mode": "key"}).status_code == 403
