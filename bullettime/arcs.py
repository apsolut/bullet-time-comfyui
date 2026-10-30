"""Camera arcs: which views become LTX keyframes, and how the generated orbit is played back.

A view is either "real" (the frozen frame of your take) or the name of a Qwen still. Every arc is played so
that it starts AND ends on the real frame, so the cut into and out of the freeze is invisible: the camera
leaves your real shot, flies around the frozen moment, and lands back exactly where the take resumes.
"""
from __future__ import annotations

# fal Qwen-Image-Edit-2511 Multiple-Angles LoRA: trained on "<sks> [azimuth] [elevation] [distance]" prompts
# at strength ~0.9. Off-format free text ("Rotate the camera 45 degrees") comes back rolled 90 degrees on
# vertical frames (found in one-shot-multicam), so every still uses the trained format.
ANGLE_LORA = 0.9
DISTANCE = "medium shot"
DISTANCES = ["close-up", "medium shot", "wide shot"]  # the LoRA's distance tokens, tight to wide


def wider(distance: str) -> str:
    """The next wider distance token (a retry that steps back; "wide shot" stays)."""
    i = DISTANCES.index(distance) if distance in DISTANCES else 1
    return DISTANCES[min(i + 1, len(DISTANCES) - 1)]

# The 8 branches of one Qwen job: name -> (azimuth, elevation). Order = branch p1..p8 = output e1..e8.
VIEWS: dict[str, tuple[str, str]] = {
    "fr": ("front-right quarter view", "eye-level shot"),
    "r": ("right side view", "eye-level shot"),
    "br": ("back-right quarter view", "eye-level shot"),
    "b": ("back view", "eye-level shot"),
    "fl": ("front-left quarter view", "eye-level shot"),
    "l": ("left side view", "eye-level shot"),
    "bl": ("back-left quarter view", "eye-level shot"),
    "up": ("front view", "high-angle shot"),
}
# Views whose face should be visible: a still where that face lies sideways is rejected.
FACE_VIEWS = {"fr", "r", "fl", "l", "up"}


def view_prompt(view: str, distance: str = DISTANCE) -> str:
    az, el = VIEWS[view]
    return f"<sks> {az} {el} {distance}"


# keys: the keyframes in generation order. gap: frames between keys (a multiple of 8, as LTX guides need).
# play: segments of the generated clip as (from_key, to_key, speed); from > to plays it backwards.
ARCS: dict[str, dict] = {
    "orbit-right": {
        "label": "Orbit right (90°)", "keys": ["real", "fr", "r"], "gap": 32,
        "move": "quarter orbit from the front to the right side of",
        "play": [(0, 2, 1.0), (2, 0, 2.0)],
    },
    "orbit-left": {
        "label": "Orbit left (90°)", "keys": ["real", "fl", "l"], "gap": 32,
        "move": "quarter orbit from the front to the left side of",
        "play": [(0, 2, 1.0), (2, 0, 2.0)],
    },
    "sweep": {
        "label": "Matrix sweep (180°, left to right)", "keys": ["l", "fl", "real", "fr", "r"], "gap": 24,
        "move": "half orbit from the left side, past the front, to the right side of",
        # real -> right, whip back through the front to the left, then home to the real frame
        "play": [(2, 4, 1.25), (4, 0, 2.5), (0, 2, 1.25)],
    },
    "half-right": {
        "label": "Half orbit behind (180°)", "keys": ["real", "fr", "r", "br", "b"], "gap": 24,
        "move": "half orbit from the front, around the right side, to behind",
        "play": [(0, 4, 1.0), (4, 0, 2.5)],
    },
    # "real_end" is the real frame again as the last keyframe, so a full turn lands back on the take
    "orbit-360": {
        "label": "Full orbit (360°)", "keys": ["real", "fr", "r", "br", "b", "bl", "l", "fl", "real_end"], "gap": 24,
        "move": "full 360 degree orbit, all the way around and back to the front of",
        "play": [(0, 8, 1.25)],
    },
    "crane": {
        "label": "Crane up", "keys": ["real", "up"], "gap": 48,
        "move": "crane move rising up and tilting down onto",
        "play": [(0, 1, 1.0), (1, 0, 2.0)],
    },
}
DEFAULT_ARC = "orbit-right"
STILL_VIEWS = list(VIEWS)  # the Qwen job always renders all 8, so any arc can be tried without a new job


def is_real(key: str) -> bool:
    return key in ("real", "real_end")


def frames(arc: str) -> int:
    a = ARCS[arc]
    return a["gap"] * (len(a["keys"]) - 1) + 1


def key_frame_idx(arc: str) -> list[int]:
    a = ARCS[arc]
    return [i * a["gap"] for i in range(len(a["keys"]))]


def playback(arc: str, fps: float = 24.0) -> list[dict]:
    """The generated clip's segments in play order: [{start, end (seconds), reverse, speed}]."""
    idx = key_frame_idx(arc)
    out = []
    for a, b, speed in ARCS[arc]["play"]:
        lo, hi = sorted((idx[a], idx[b]))
        out.append({"start": lo / fps, "end": hi / fps, "reverse": a > b, "speed": float(speed)})
    return out


def freeze_seconds(arc: str, fps: float = 24.0) -> float:
    return round(sum((s["end"] - s["start"]) / s["speed"] for s in playback(arc, fps)), 3)


def available(arc: str, ok_views: set[str]) -> bool:
    """An arc needs its real frame plus at least its last key view (middle keys may be dropped)."""
    keys = ARCS[arc]["keys"]
    ends = [k for k in (keys[0], keys[-1]) if not is_real(k)]
    return all(k in ok_views for k in ends)
