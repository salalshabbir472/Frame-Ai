"""
config.py - settings + small helpers (NO Gemini / network calls here).

Contains: model chains, LANGS, VideoConfig (the single control surface) and generic
helpers (env loading, ffprobe duration, slugify, JSON-fence stripping, disk cache...).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("edu")

# Windows consoles / files default to cp1252, which cannot hold Urdu: force UTF-8 everywhere.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

QUALITY_FLAGS = {"low": "-ql", "medium": "-qm", "high": "-qh"}
QUALITY_SHORT = {"low": 480, "medium": 720, "high": 1080}   # short side of the rendered frame in pixels
OUT_SHORT = 720                                             # short side of the final video (16:9 -> 1280x720)

# Frame shape. Merge order is the same as every other setting: row > project > this default.
ASPECTS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5)}
DEFAULT_ASPECT = "16:9"
_ASPECT_WORDS = {"wide": "16:9", "landscape": "16:9", "horizontal": "16:9", "youtube": "16:9",
                 "vertical": "9:16", "portrait": "9:16", "shorts": "9:16", "reels": "9:16", "tiktok": "9:16",
                 "square": "1:1"}


def normalize_aspect(s) -> str:
    """'9:16' | '9x16' | '9/16' | ' 9 : 16 ' | 'vertical' -> '9:16'. Anything unknown -> '' (not valid)."""
    t = str(s or "").strip().lower()
    if t in _ASPECT_WORDS:
        return _ASPECT_WORDS[t]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[:x/\u00d7]\s*(\d+(?:\.\d+)?)", t)
    if not m:
        return ""
    key = f"{float(m.group(1)):g}:{float(m.group(2)):g}"
    return key if key in ASPECTS else ""


def pixel_size(aspect: str, short: int):
    """(width, height) in pixels whose SHORT side is `short`, both rounded to even numbers (H.264)."""
    w, h = ASPECTS[aspect]
    even = lambda v: int(round(v / 2.0)) * 2   # noqa: E731
    return (even(short * w / h), even(short)) if w >= h else (even(short), even(short * h / w))


def frame_units(aspect: str):
    """Manim frame size in scene units. Wide keeps Manim's own 8-unit height. Vertical / square / portrait fix the
    WIDTH at 8 units instead, so text that fits a wide frame looks about twice as big and layouts stack top to bottom."""
    w, h = ASPECTS[aspect]
    return (8.0 * w / h, 8.0) if w > h else (8.0, 8.0 * h / w)

# --------------------------------------------------------------------------- #
# Free-tier model chains (Google pricing page, Oct 2026: all of these list
# "Free of charge"). Free Flash models have small daily request caps, Flash-Lite has
# a big one -> cheap JSON work goes to Flash-Lite first, code goes to Flash first, and
# the long Flash chain multiplies the daily allowance (limits are per model).
# Models your project cannot use (404/403 "model") are dropped automatically.
# Check your real limits: https://aistudio.google.com/rate-limit
# --------------------------------------------------------------------------- #
DEFAULT_LIGHT_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
)

DEFAULT_MEDIUM_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
)

DEFAULT_CODE_MODELS = (
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3-flash-preview",
)

DEFAULT_TTS_MODELS = (
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
)

DEFAULT_TRANSCRIPTION_MODELS = (
    "gemini-3.5-transcribe",
    "gemini-3.5-transcribe-live",
)

LANGS = {
    "en": dict(name="English", wps=2.5, rtl=False,
               cloud_voices="en-US-Neural2-D,en-US-Standard-D"),
    # Urdu is NOT in Google's official Gemini-TTS language list (checked 2026-10-07):
    # Gemini auto-detects language, so it may still speak it, but test with --tts-test.
    "ur": dict(name="Urdu", wps=2.3, rtl=True,
               cloud_voices="ur-IN-Wavenet-A,ur-IN-Standard-A"),
}
SENT_SPLIT = re.compile(r"(?<=[.!?؟۔…])\s+")


# --------------------------------------------------------------------------- #
# The single control surface
# --------------------------------------------------------------------------- #
def _env(name, default):
    return lambda: os.environ.get(name, default)


@dataclass
class VideoConfig:
    topic: str
    # content
    minutes: float = 3.0
    audience: str = "curious beginners"
    language: str = "en"             # narration + script + TTS: "en" | "ur"
    screen_language: str = ""        # on-screen text; "" = same as `language`
    script: str = ""                 # a finished script (narration used word for word); "" = Frame writes it
    input_type: str = ""             # "" = auto (script if `script` is filled) | prompt | script
    script_cleanup: bool = True      # own script only: Gemini removes notes/metadata and fixes punctuation, never rewords
    # voice
    voice: str = "Charon"            # Gemini prebuilt voice (Kore, Puck, Charon, Fenrir, Aoede...)
    tts_style: str = "calm, clear and friendly, like a good teacher"
    tts_engine: str = "auto"         # auto = Gemini then Cloud TTS | gemini | cloud
    cloud_voices: str = ""           # "" = per-language default
    # models
    light_models: str = field(default_factory=_env("GEMINI_MODELS_LIGHT", DEFAULT_LIGHT_MODELS))
    medium_models: str = field(default_factory=_env("GEMINI_MODELS_MEDIUM", DEFAULT_MEDIUM_MODELS))
    code_models: str = field(default_factory=_env("GEMINI_MODELS_CODE", DEFAULT_CODE_MODELS))
    tts_models: str = field(default_factory=_env("GEMINI_MODELS_TTS", DEFAULT_TTS_MODELS))
    rpm: float = field(default_factory=lambda: float(os.environ.get("GEMINI_RPM", 5)))
    tts_rpm: float = field(default_factory=lambda: float(os.environ.get("GEMINI_TTS_RPM", 4)))
    tts_group_scenes: int = 4        # max scenes sent to Gemini TTS in ONE call (1 = no batching)
    tts_group_chars: int = 4000      # max total characters per TTS call
    max_wait: float = 240.0          # max seconds to sleep for a quota window
    # visuals
    scene_sec: float = 15.0          # target seconds per scene (smaller = more scenes)
    chapter_min: float = 2.0         # script is planned in chapters of ~this many minutes (videos > 4 min)
    scene_batch: int = 4             # scenes whose narration is written per Gemini call
    max_scenes: int = 60
    min_steps_per_sentence: int = 2
    # video
    quality: str = "medium"          # low | medium | high
    aspect: str = DEFAULT_ASPECT     # frame shape: 16:9 (wide) | 9:16 (vertical) | 1:1 | 4:5
    retries: int = 2                 # Manim auto-repair attempts per scene
    allow_latex: bool = False
    urdu_font: str = "Noto Naskh Arabic"
    workdir: str = "build"
    output_dir: str = "output"

    def __post_init__(self):
        self.language = self.language.lower()
        self.screen_language = (self.screen_language or self.language).lower()
        for lang in (self.language, self.screen_language):
            if lang not in LANGS:
                raise ValueError(f"language must be one of {sorted(LANGS)}, got {lang!r}")
        if self.quality not in QUALITY_FLAGS:
            raise ValueError(f"quality must be one of {sorted(QUALITY_FLAGS)}")
        self.aspect = normalize_aspect(self.aspect or DEFAULT_ASPECT)
        if not self.aspect:
            raise ValueError(f"aspect must be one of {sorted(ASPECTS)}")
        if self.tts_engine not in ("auto", "gemini", "cloud"):
            raise ValueError("tts_engine must be auto | gemini | cloud")
        if self.chapter_min <= 0 or self.scene_batch < 1 or self.tts_group_scenes < 1:
            raise ValueError("chapter_min must be > 0; scene_batch and tts_group_scenes >= 1")
        if not self.topic or not self.topic.strip():
            raise ValueError("topic is empty")
        self.script = (self.script or "").strip()
        self.input_type = (self.input_type or ("script" if self.script else "prompt")).strip().lower()
        if self.input_type not in ("prompt", "script"):
            raise ValueError("input_type must be prompt | script")
        if self.input_type == "script" and not self.script:
            raise ValueError("Input Type is Script but the Script cell is empty")
        if self.screen_language == "ur":
            self.allow_latex = False   # no LaTeX with Urdu text

    @property
    def is_wide(self) -> bool:
        return ASPECTS[self.aspect][0] > ASPECTS[self.aspect][1]

    @property
    def frame_size(self):
        """Manim frame (width, height) in scene units."""
        return frame_units(self.aspect)

    @property
    def render_size(self):
        """Pixels Manim renders at (follows `quality`)."""
        return pixel_size(self.aspect, QUALITY_SHORT[self.quality])

    @property
    def out_size(self):
        """Pixels of the final video: 1280x720 for 16:9 (as always), 720x1280 for 9:16, ..."""
        return pixel_size(self.aspect, OUT_SHORT)

    @property
    def slug(self):
        # slugify() cuts at 40 chars: two long topics that start the same would share a work
        # folder (and reuse each other's cached script!), so add a short hash when it was cut.
        full = re.sub(r"[^a-z0-9]+", "-", self.topic.lower()).strip("-")
        base = slugify(self.topic)
        if len(full) > 40:
            base = base[:31].rstrip("-") + "-" + hashlib.md5(self.topic.strip().encode()).hexdigest()[:8]
        if self.input_type == "script":   # same title, edited script -> new work folder (no stale cached script)
            base += "-s" + hashlib.md5(self.script.encode()).hexdigest()[:6]
        shape = "" if self.aspect == DEFAULT_ASPECT else "-" + self.aspect.replace(":", "x")   # 16:9 keeps the old name
        return f"{base}-{self.language}{shape}"   # a vertical and a wide version of one topic never share a folder


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def detect_language(text: str):
    """'ur' when the letters are mostly Arabic/Urdu script, 'en' when they are almost all Latin, else None."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 20:
        return None
    ar = sum("\u0600" <= c <= "\u06ff" or "\u0750" <= c <= "\u077f" or "\ufb50" <= c <= "\ufdff"
             or "\ufe70" <= c <= "\ufeff" for c in letters) / len(letters)
    return "ur" if ar > 0.5 else "en" if ar < 0.1 else None


def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env_list(*names):
    out = []
    for n in names:
        for part in os.environ.get(n, "").replace("\n", ",").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def csv(s):
    if isinstance(s, (list, tuple)):
        return [str(x).strip() for x in s if str(x).strip()]
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def run(cmd, **kw):
    # ffmpeg / manim output can contain Urdu: decode as UTF-8, never crash on odd bytes
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)])
    return float(r.stdout.strip())


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or \
        "topic-" + hashlib.md5(s.encode()).hexdigest()[:8]   # Urdu titles have no a-z


def strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json|python)?\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


def as_scenes(raw):
    """Gemini sometimes returns a bare list, or a dict under another key. Always give back
    a list of scene dicts."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("scenes")
        if items is None:
            lists = [v for v in raw.values() if isinstance(v, list)]
            items = lists[0] if lists else []
    else:
        items = []
    return [x for x in items if isinstance(x, dict)]


def load_json(path, default=None):
    """Read a JSON file; a missing, empty or half-written file gives `default` (never a crash)."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log.warning("  ignoring unreadable %s (%s); it will be rebuilt", path.name, e)
        return default


def save_json(path, data):
    """Write to a temp file, then swap it in: a crash can never leave a truncated JSON behind."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def cached(path: Path, fn):
    data = load_json(path)
    if data is not None:
        log.info("  (cached) %s", path.name)
        return data
    data = fn()
    save_json(path, data)
    return data


def secs_to_pacific_midnight() -> float:
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return (nxt - now).total_seconds()


def split_sentences(text: str):
    parts = [s.strip() for s in SENT_SPLIT.split(text.strip()) if s.strip()]
    return parts or [text.strip()]
