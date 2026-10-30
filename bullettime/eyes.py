"""Are the eyes open? A frozen frame with closed eyes (a blink, or mid-word) poisons everything after it:
Qwen copies the closed eyes into all 8 views and LTX has no real eyes to carry through the orbit, so the face
drifts. Google MediaPipe Face Landmarker (open model, Apache-2.0) gives per-eye blink scores (blendshapes).

The model (3.7 MB) is downloaded once to ~/.bullettime/models. Without mediapipe or the model every function
returns None and the picker falls back to motion x sharpness alone.
"""
from __future__ import annotations

import os
from pathlib import Path

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/"
             "face_landmarker.task")
MODEL = Path(os.environ.get("BULLETTIME_MODELS", Path.home() / ".bullettime" / "models")) / "face_landmarker.task"
CLOSED = 0.45  # blink score above this (either eye) = closed or closing


def _model() -> Path | None:
    if MODEL.exists():
        return MODEL
    try:
        import requests
        r = requests.get(MODEL_URL, timeout=60)
        r.raise_for_status()
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        tmp = MODEL.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(MODEL)
        return MODEL
    except Exception:  # noqa: BLE001
        return None


def _landmarker(video: bool):
    try:
        import mediapipe as mp  # noqa: F401
        from mediapipe.tasks.python import BaseOptions, vision
    except Exception:  # noqa: BLE001
        return None
    m = _model()
    if not m:
        return None
    try:
        mode = vision.RunningMode.VIDEO if video else vision.RunningMode.IMAGE
        return vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(m)), running_mode=mode, output_face_blendshapes=True,
            num_faces=1))
    except Exception:  # noqa: BLE001
        return None


def _blink(result) -> float | None:
    if not result.face_blendshapes:
        return None
    d = {c.category_name: c.score for c in result.face_blendshapes[0]}
    return max(d.get("eyeBlinkLeft", 0.0), d.get("eyeBlinkRight", 0.0))


def _rgb(frame_bgr, max_side=480):
    import cv2
    h, w = frame_bgr.shape[:2]
    s = min(1.0, max_side / max(h, w))
    return cv2.cvtColor(cv2.resize(frame_bgr, (max(1, int(w * s)), max(1, int(h * s)))), cv2.COLOR_BGR2RGB)


def blink_curve(video: str | Path, step: int = 2) -> list[float | None] | None:
    """Blink score per frame of `video` (0 = wide open, 1 = shut), measured every `step` frames and held in
    between; None where no face is found. None for the whole clip if the detector isn't available."""
    lm = _landmarker(video=True)
    if lm is None:
        return None
    import cv2
    import mediapipe as mp
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    out, i, last = [], 0, None
    try:
        while True:
            ok, f = cap.read()
            if not ok:
                break
            if i % step == 0:
                img = mp.Image(image_format=mp.ImageFormat.SRGB, data=_rgb(f))
                last = _blink(lm.detect_for_video(img, int(i * 1000 / fps)))
            out.append(last)
            i += 1
    finally:
        cap.release()
        lm.close()
    return out


def blink_image(path: str | Path) -> float | None:
    """Blink score of one image, or None (no face / no detector)."""
    lm = _landmarker(video=False)
    if lm is None:
        return None
    import cv2
    import mediapipe as mp
    try:
        f = cv2.imread(str(path))
        if f is None:
            return None
        return _blink(lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=_rgb(f))))
    finally:
        lm.close()


def closed_frames(blink: list[float | None] | None, pad: int = 2) -> set[int]:
    """Frames whose eyes are closed, widened by `pad` frames each side (a blink is closing before it shows)."""
    if not blink:
        return set()
    hit = [i for i, b in enumerate(blink) if b is not None and b > CLOSED]
    return {j for i in hit for j in range(max(0, i - pad), min(len(blink), i + pad + 1))}


def nearest_open(blink: list[float | None] | None, frame: int, max_shift: int = 15) -> int:
    """`frame` if its eyes are open, else the nearest open frame (later first, as a blink reopens forwards),
    within max_shift frames. Unknown eyes (no detector, no face) count as open."""
    closed = closed_frames(blink)
    if frame not in closed:
        return frame
    for d in range(1, max_shift + 1):
        for f in (frame + d, frame - d):
            if 0 <= f < len(blink) and f not in closed:
                return f
    return frame
