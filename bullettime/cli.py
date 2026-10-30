"""bullettime CLI.

    bullettime run take.mp4 --arc sweep            # everything: pick, stills, orbit, render (~$0.10)
    bullettime analyze take.mp4                    # free: where would it freeze?
    bullettime -P work new take.mp4 --t 2.4        # step by step, in project folder work/
    bullettime -P work stills | orbit --arc orbit-left | render --ramp 0.6
    bullettime workflow --arc sweep > sweep_api.json   # the exact LTX graph, for ComfyUI
    bullettime serve                               # web app on http://127.0.0.1:8790
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import arcs, comfy, pick, pipeline, workflows


def _log(msg: str) -> None:
    print(msg, flush=True)


def _status(rec: dict) -> None:
    print(f"  {rec.get('status')} {rec.get('prompt_id', '')}", flush=True)


def _project(args) -> pipeline.Project:
    p = pipeline.Project(args.project)
    if not p.state:
        sys.exit(f"error: no project in {args.project} (run `bullettime -P {args.project} new VIDEO` first)")
    return p


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="bullettime", description="Bullet time from one phone shot (ComfyUI, open weights)")
    ap.add_argument("-P", "--project", default="work", help="project folder (default: work)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="pick the freeze moment (free)")
    a.add_argument("video")

    n = sub.add_parser("new", help="start a project from a take (free)")
    n.add_argument("video")
    n.add_argument("--t", type=float, help="freeze at this time in seconds (default: auto)")

    s = sub.add_parser("stills", help="8 views of the frozen frame (1 Qwen job)")
    s.add_argument("--t", type=float)
    s.add_argument("--seed", type=int)
    s.add_argument("--quality", action="store_true")

    rv = sub.add_parser("retry-views", help="re-roll the rejected views (or --views) with new seeds, ~$0.025 each")
    rv.add_argument("--views", nargs="*", choices=list(arcs.VIEWS))
    rv.add_argument("--seed", type=int)

    up = sub.add_parser("upscale", help="SeedVR2 upscale of the current orbit (1 job, ~$0.12), then re-render")
    up.add_argument("--short-side", type=int, default=1080)

    se = sub.add_parser("select", help="make one of the generated orbits current (see `bullettime -P work status`)")
    se.add_argument("seed", type=int)

    sub.add_parser("status", help="project summary: views, orbits, spend")
    ex = sub.add_parser("export", help="studio bundle: ProRes master, loop, frame, views, timeline.json, graphs")
    ex.add_argument("--no-master", action="store_true", help="skip the ProRes master")

    for name, hlp in (("orbit", "the camera move through frozen time (1 LTX job)"),
                      ("run", "new + stills + orbit + render in one go")):
        o = sub.add_parser(name, help=hlp)
        if name == "run":
            o.add_argument("video")
            o.add_argument("--t", type=float)
            o.add_argument("--out", help="copy the result here")
        o.add_argument("--arc", default=arcs.DEFAULT_ARC, choices=list(arcs.ARCS))
        o.add_argument("--seed", type=int)
        o.add_argument("--subject", default="", help='who is frozen, e.g. "the woman in the red jacket"')
        o.add_argument("--sound", default="whoosh", choices=["whoosh", "silent"])
        o.add_argument("--guide", type=float, default=workflows.GUIDE_STRENGTH,
                       help="keyframe strength of the generated views (lower = smoother, looser)")
        o.add_argument("--color-match", type=float, default=0.0, metavar="0..1",
                       help="match the orbit's colours to the real frame (0 = off)")
        o.add_argument("--variants", type=int, default=1, metavar="N", help="N orbits with different seeds at once")
        o.add_argument("--quality", action="store_true", help="40-step views instead of Lightning 4-step (~5x view cost)")
        o.add_argument("--no-fix-views", action="store_true", help="don't re-roll rejected views before the orbit")
        o.add_argument("--upscale", action="store_true", help="SeedVR2 upscale of the orbit before the splice")
        o.add_argument("--dry-run", action="store_true", help="print the plan and cost, spend nothing")

    r = sub.add_parser("render", help="splice the orbit into the take (free)")
    r.add_argument("--ramp", type=float, default=0.4, help="slow-mo ramp into the freeze, seconds (0 = off)")
    r.add_argument("--slow", type=float, default=3.0, help="ramp slow-down factor")
    r.add_argument("--sound", choices=["whoosh", "silent"])

    w = sub.add_parser("workflow", help="print the LTX orbit graph (API format)")
    w.add_argument("--arc", default=arcs.DEFAULT_ARC, choices=list(arcs.ARCS))
    w.add_argument("--width", type=int, default=704)
    w.add_argument("--height", type=int, default=1280)

    sv = sub.add_parser("serve", help="run the web app")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8790)
    sv.add_argument("--data", default=os.environ.get("BULLETTIME_DATA") or str(Path.home() / ".bullettime"))

    sub.add_parser("arcs", help="list the camera arcs")
    args = ap.parse_args(argv)
    if getattr(args, "project", None):  # keep every submitted graph next to the project, for `export`
        comfy.set_workflow_archive(Path(args.project) / "workflows", keep_s=365 * 86400)

    if args.cmd == "analyze":
        res = pick.analyze(args.video)
        print(json.dumps({k: res[k] for k in ("t", "frame", "fps", "duration")}, indent=2))
    elif args.cmd == "arcs":
        for k, v in arcs.ARCS.items():
            print(f"{k:12s} {v['label']:36s} keys={v['keys']}  freeze={arcs.freeze_seconds(k)}s")
    elif args.cmd == "workflow":
        wf, _ = workflows.build_orbit(args.arc, args.width, args.height)
        wf["5"]["inputs"]["text"] = workflows.orbit_prompt(args.arc)
        print(json.dumps(wf, indent=1))
    elif args.cmd == "new":
        p = pipeline.new_project(args.video, Path(args.project).parent, Path(args.project).name)
        if args.t is not None:
            p.state["t"] = args.t; p.save()
        print(f"project {p.dir}: freeze at {p.state['t']:.2f}s (auto pick {p.state['analysis']['t']:.2f}s)")
    elif args.cmd == "stills":
        p = _project(args)
        st = pipeline.stills(p, args.t, seed=args.seed, log=_log, on_status=_status, quality=args.quality)
        for v, rec in st["views"].items():
            print(f"  {v:3s} {'ok ' if rec['ok'] else 'BAD'} {rec['path']} {'' if rec['ok'] else rec['reason']}")
    elif args.cmd in ("orbit", "run"):
        if args.cmd == "run":
            proj = Path(args.project)
            p = pipeline.new_project(args.video, proj.parent, proj.name)
            if args.t is not None:
                p.state["t"] = args.t; p.save()
            print(f"freeze at {p.state['t']:.2f}s")
        else:
            p = _project(args)
        est = pipeline.estimate(p, quality=args.quality, n_variants=args.variants, upscale=args.upscale)
        print(f"{args.arc}: {' + '.join(est['jobs'])} ≈ ${est['usd']:.3f} on Comfy Cloud, "
              f"freeze lasts {arcs.freeze_seconds(args.arc)}s")
        ey = (p.state.get("analysis") or {}).get("eyes")
        if ey and not ey["ok"]:
            print(f"  warning: eye check off ({ey['error']}); the freeze may land on a blink")
        if args.dry_run:
            return
        if not comfy.env_key():
            sys.exit("error: set COMFY_API_KEY (https://platform.comfy.org/profile/api-keys)")
        if "stills" in est["jobs"] or "stills_quality" in est["jobs"]:
            pipeline.stills(p, seed=args.seed, log=_log, on_status=_status, quality=args.quality)
            bad = {v: r["reason"] for v, r in p.state["stills"]["views"].items() if not r["ok"]}
            if bad:
                print(f"  rejected stills: {bad}")
        if not args.no_fix_views and any(not r["ok"] for r in p.state["stills"]["views"].values()):
            pipeline.retry_views(p, seed=args.seed, log=_log, on_status=_status)
        kw = dict(subject=args.subject, sound=args.sound, log=_log, guide=args.guide, color_match=args.color_match)
        if args.variants > 1:
            outs = pipeline.variants(p, args.arc, args.variants, seed=args.seed,
                                     on_status=lambda i, rec: _status({**rec, "status": f"variant {i + 1}: {rec.get('status')}"}), **kw)
            print("  variants: " + ", ".join(f"seed {o['seed']}" for o in outs) + f" (current: {outs[0]['seed']}; `bullettime -P {args.project} select SEED` to switch)")
        else:
            pipeline.orbit(p, args.arc, seed=args.seed, on_status=_status, **kw)
        if args.upscale:
            pipeline.upscale(p, log=_log, on_status=_status)
        if args.cmd == "run":
            rep = pipeline.render(p, log=_log)
            out = p.path(rep["out"])
            if args.out:
                Path(args.out).write_bytes(out.read_bytes()); out = Path(args.out)
            print(f"done: {out} ({rep['duration']:.1f}s)")
    elif args.cmd == "render":
        p = _project(args)
        rep = pipeline.render(p, ramp_s=args.ramp, ramp_slow=args.slow, sound=args.sound, log=_log)
        print(f"done: {p.path(rep['out'])} ({rep['duration']:.1f}s), loop: {p.path(rep['orbit_only'])}")
    elif args.cmd == "retry-views":
        p = _project(args)
        st = pipeline.retry_views(p, args.views or None, seed=args.seed, log=_log, on_status=_status)
        print("  " + ", ".join(f"{v}: {'ok' if r['ok'] else r['reason']}" for v, r in st["views"].items()))
    elif args.cmd == "upscale":
        p = _project(args)
        pipeline.upscale(p, short_side=args.short_side, log=_log, on_status=_status)
        rep = pipeline.render(p, log=_log)
        print(f"done: {p.path(rep['out'])} (orbit clip: {rep['orbit_clip']})")
    elif args.cmd == "select":
        p = _project(args)
        o = pipeline.select_orbit(p, args.seed)
        rep = pipeline.render(p, log=_log)
        print(f"current orbit: {o['arc']} seed {o['seed']} -> {p.path(rep['out'])}")
    elif args.cmd == "status":
        p = _project(args)
        s = p.state
        print(f"project {p.dir}: take {s['take']} {s['info']['duration']:.1f}s, freeze at {s['t']:.2f}s, spent ${pipeline.spent_usd(p):.3f}")
        for v, r in ((s.get("stills") or {}).get("views") or {}).items():
            print(f"  view {v:3s} {'ok ' if r['ok'] else 'BAD'} {r['path']} {'' if r['ok'] else r['reason']}")
        cur = (s.get("orbit") or {}).get("path")
        for o in s.get("orbits", []):
            print(f"  orbit {o['arc']:12s} seed {o['seed']:<16d} {'* ' if o['path'] == cur else '  '}{o['path']}{' (+upscaled)' if o.get('path_up') else ''}")
        if s.get("render"):
            print(f"  render {p.path(s['render']['out'])} ({s['render']['duration']:.1f}s)")
    elif args.cmd == "export":
        p = _project(args)
        z = pipeline.export(p, master=not args.no_master, log=_log)
        print(f"done: {z}")
    elif args.cmd == "serve":
        import uvicorn
        from .server import create_app
        uvicorn.run(create_app(args.data), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
