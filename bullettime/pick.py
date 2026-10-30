"""Pick the freeze moment: the most dramatic frame that is still sharp.

Bullet time looks best at the peak of an action (a jump, a hair flip, a punch, a splash). Peak motion is
also where frames blur, and a blurred freeze frame becomes a blurred statue, so the score is
motion x sharpness: the sharpest frame close to a motion peak wins. Frames with closed eyes (blinks, mid-word)
are ruled out with MediaPipe Face Landmarker (see eyes.py). CPU only, a few seconds for a 15 s clip.
"""
from __future__ import annotations

from pathlib import Path


def analyze(video: str | Path, max_side: int = 320, margin_s: float = 0.6, use_eyes: bool = True) -> dict:
    """-> {t, fps, duration, times[], motion[], sharpness[], score[]} (curves normalized to 0..1)."""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    prev, motion, sharp = None, [], []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        s = max_side / max(h, w)
        g = cv2.cvtColor(cv2.resize(frame, (max(1, int(w * s)), max(1, int(h * s)))), cv2.COLOR_BGR2GRAY)
        motion.append(0.0 if prev is None else float(np.mean(cv2.absdiff(g, prev))))
        sharp.append(float(cv2.Laplacian(g, cv2.CV_64F).var()))
        prev = g
    cap.release()
    nf = len(motion)
    if nf == 0:
        raise ValueError(f"no frames could be read from {video}")
    dur = nf / fps
    m = np.array(motion)
    if nf > 1:
        m[0] = m[1]
    k = max(1, int(round(fps * 0.25)))  # smooth over ~0.25 s: one jittery frame is not an action peak
    m_s = np.convolve(m, np.ones(k) / k, mode="same")
    sh = np.array(sharp)

    def norm(x):
        lo, hi = float(np.min(x)), float(np.max(x))
        return (x - lo) / (hi - lo) if hi > lo else np.zeros_like(x)

    mn, shn = norm(m_s), norm(sh)
    score = mn * (0.35 + 0.65 * shn)
    # never freeze on closed eyes: Qwen would copy them into every view and the face drifts in the orbit
    from . import eyes
    blink = eyes.blink_curve(video) if use_eyes else None
    closed = eyes.closed_frames(blink)
    if closed:
        score[[i for i in sorted(closed) if i < nf]] *= 0.02
    lo_i = min(nf - 1, int(margin_s * fps))
    hi_i = max(lo_i + 1, nf - int(margin_s * fps))
    window = score[lo_i:hi_i] if hi_i > lo_i else score
    best = int(np.argmax(window)) + (lo_i if hi_i > lo_i else 0)
    if not np.any(window):  # a static clip: freeze in the middle
        best = eyes.nearest_open(blink, nf // 2)
    step = max(1, nf // 200)  # curves for the UI sparkline, <= ~200 points

    def r(x):
        return [round(float(v), 3) for v in x[::step]]
    return {"t": round(best / fps, 3), "fps": fps, "duration": round(dur, 3), "frame": best,
            "times": [round(i / fps, 3) for i in range(0, nf, step)], "motion": r(mn), "sharpness": r(shn),
            "score": r(score), "blink": [None if b is None else round(b, 2) for b in blink] if blink else None,
            "eyes_closed_at_pick": best in closed}
