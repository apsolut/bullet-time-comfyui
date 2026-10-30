"""Splice the orbit into the real take with ffmpeg (free, CPU).

    real take ..... | slow-mo ramp into t | orbit (frozen time) | rewind home | real take from t .....
    real audio .... | real audio, pitched  | generated whoosh    | reversed    | real audio ...........

The real take is never re-timed outside the ramp, so the speech before and after the freeze stays in sync
with its own audio. The orbit starts and ends on the frozen frame, so both cuts land on the same image.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import arcs
from .util import need, probe, run

SR = 48000
WHOOSH_DB = -3.0   # the orbit's generated audio sits this far under the take's average level (never a boost)
CUT_FADE = 0.15    # s, fade on each orbit-audio segment so the cuts into and out of the whoosh don't click


def mean_volume_db(path: str | Path) -> float | None:
    """Average level of a file's audio in dBFS (ffmpeg volumedetect), None when it has no audio."""
    r = run(["ffmpeg", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr or "")
    return float(m.group(1)) if m else None


def whoosh_gain_db(take: str | Path, orbit: str | Path, whoosh_db: float = WHOOSH_DB) -> float:
    """Gain for the orbit audio so its average lands whoosh_db under the take's voice: LTX writes the whoosh
    near full scale while a phone take's voice averages around -35 dB, a 20+ dB jump at the freeze."""
    t, o = mean_volume_db(take), mean_volume_db(orbit)
    if t is None or o is None:
        return -26.0  # ~5% volume, the level that worked for the promo cut
    return max(-40.0, min(0.0, t - o + whoosh_db))


def out_size(w: int, h: int, long_side: int = 1920) -> tuple[int, int]:
    s = min(1.0, long_side / max(w, h))
    return int(w * s) // 2 * 2, int(h * s) // 2 * 2


def _atempo(speed: float) -> str:
    """atempo chain for any speed (each stage 0.5..2.0, as old ffmpeg builds require)."""
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0"); s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5"); s /= 0.5
    parts.append(f"atempo={s:.5f}")
    return ",".join(parts)


def plan(src_info: dict, t: float, arc: str, ramp_s: float = 0.4, ramp_slow: float = 3.0) -> list[dict]:
    """The output timeline: [{kind, src: 'take'|'orbit', start, end, reverse, speed}] (speed = source s / output s)."""
    dur = src_info["duration"]
    t = min(max(t, 0.05), dur - 0.05)
    ramp_s = max(0.0, min(ramp_s, t - 0.02)) if ramp_slow > 1 else 0.0
    segs = []
    if t - ramp_s > 0.02:
        segs.append({"kind": "take", "src": "take", "start": 0.0, "end": round(t - ramp_s, 4), "reverse": False, "speed": 1.0})
    if ramp_s > 0:
        segs.append({"kind": "ramp", "src": "take", "start": round(t - ramp_s, 4), "end": round(t, 4), "reverse": False,
                     "speed": round(1.0 / ramp_slow, 4)})
    for p in arcs.playback(arc):
        segs.append({"kind": "orbit", "src": "orbit", **p})
    segs.append({"kind": "take", "src": "take", "start": round(t, 4), "end": round(dur, 4), "reverse": False, "speed": 1.0})
    return segs


def render(take: str | Path, orbit: str | Path, t: float, arc: str, out: str | Path, ramp_s: float = 0.4,
           ramp_slow: float = 3.0, sound: str = "whoosh", fps: int = 30, long_side: int = 1920,
           orbit_only: str | Path | None = None, whoosh_db: float = WHOOSH_DB) -> dict:
    """Render the final video. sound: "whoosh" (the orbit's generated audio, levelled whoosh_db under the take's
    voice) or "silent". -> render report."""
    need("ffmpeg")
    info = probe(take)
    oinfo = probe(orbit)
    W, H = out_size(info["width"], info["height"], long_side)
    segs = plan(info, t, arc, ramp_s, ramp_slow)
    take_audio = "sample_rate" in info
    orbit_audio = "sample_rate" in oinfo and sound == "whoosh"
    n_take = sum(s["src"] == "take" for s in segs)
    n_orb = len(segs) - n_take
    f = [f"[0:v]split={n_take}" + "".join(f"[tv{i}]" for i in range(n_take)),
         f"[1:v]split={n_orb}" + "".join(f"[ov{i}]" for i in range(n_orb))]
    if take_audio:
        f.append(f"[0:a]aformat=sample_rates={SR}:channel_layouts=stereo,asplit={n_take}" + "".join(f"[ta{i}]" for i in range(n_take)))
    gain_db = whoosh_gain_db(take, orbit, whoosh_db) if orbit_audio else None
    if orbit_audio:
        f.append(f"[1:a]aformat=sample_rates={SR}:channel_layouts=stereo,volume={gain_db:.2f}dB,asplit={n_orb}" + "".join(f"[oa{i}]" for i in range(n_orb)))
    vfit = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1,"
            f"fps={fps},format=yuv420p")
    ti = oi = 0
    labels = []
    total = 0.0
    for k, s in enumerate(segs):
        length = (s["end"] - s["start"]) / s["speed"]
        total += length
        if s["src"] == "take":
            vin, ain = f"[tv{ti}]", f"[ta{ti}]" if take_audio else None
            ti += 1
            end = s["end"]
        else:
            vin, ain = f"[ov{oi}]", f"[oa{oi}]" if orbit_audio else None
            oi += 1
            end = s["end"] + 0.5 / 24.0  # include the key frame itself
        rev = ",reverse" if s["reverse"] else ""
        f.append(f"{vin}trim=start={s['start']:.4f}:end={end:.4f},setpts=PTS-STARTPTS{rev},"
                 f"setpts=(PTS-STARTPTS)/{s['speed']:.5f},{vfit},trim=duration={length:.4f}[v{k}]")
        if ain:
            arev = ",areverse" if s["reverse"] else ""
            if s["kind"] == "ramp":  # slow AND pitched down: time stretching into the freeze
                tempo = f"asetrate={int(SR * s['speed'])},aresample={SR}"
            else:
                tempo = _atempo(s["speed"])
            fade = ""
            if s["src"] == "orbit":  # soften the cuts into and out of the generated whoosh
                fd = min(CUT_FADE, length / 2)
                fade = f",afade=t=in:st=0:d={fd:.3f},afade=t=out:st={max(0.0, length - fd):.4f}:d={fd:.3f}"
            f.append(f"{ain}atrim=start={s['start']:.4f}:end={s['end']:.4f},asetpts=PTS-STARTPTS{arev},{tempo},"
                     f"apad,atrim=duration={length:.4f}{fade}[a{k}]")
        else:
            f.append(f"anullsrc=r={SR}:cl=stereo,atrim=duration={length:.4f}[a{k}]")
        labels.append(f"[v{k}][a{k}]")
    f.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=1[vout][aout]")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-i", str(take), "-i", str(orbit), "-filter_complex", ";".join(f),
         "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)])
    report = {"out": str(out), "width": W, "height": H, "fps": fps, "duration": round(total, 3), "segments": segs,
              "freeze_at": round(t, 3), "arc": arc, "sound": sound, "whoosh_gain_db": gain_db}
    if orbit_only:
        # the freeze on its own: ramp + orbit + rewind, for a loopable share clip
        lo = sum((s["end"] - s["start"]) / s["speed"] for s in segs[:1] if s["kind"] == "take")
        hi = total - (segs[-1]["end"] - segs[-1]["start"])
        run(["ffmpeg", "-y", "-ss", f"{lo:.3f}", "-to", f"{hi:.3f}", "-i", str(out), "-c:v", "libx264", "-crf", "18",
             "-c:a", "aac", "-movflags", "+faststart", str(orbit_only)])
        report["orbit_only"] = str(orbit_only)
    return report
