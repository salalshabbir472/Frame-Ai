"""
scene_video.py - scene descriptions in, one MP4 out.

    from scene_video import generate_video
    path = generate_video("Scene 1: nested boxes show a function calling itself\n"
                          "Scene 2: a call stack grows with each recursive call")

Pipeline (no script, no voice):
    scene description -> Gemini writes Manim code -> validate (AST) -> render -> on error
    Gemini repairs the code (limited retries) -> scenes concatenated with ffmpeg -> final MP4.

Reuses config.py (VideoConfig, helpers) and gemini_client.py (key/model rotation) as they are.
Needs: pip install manim google-genai   and   ffmpeg on PATH.

SECURITY NOTE: generated code is checked with an AST allow-list (only manim/math/random/numpy imports,
no open/exec/eval/os/subprocess, no `config` access) and runs in a separate process with a time limit.
That reduces risk but is NOT a real sandbox. Run it in a container/VM if you need real isolation.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from config import VideoConfig, csv, duration, env_list, load_env, run, strip_fences
from gemini_client import Gemini

log = logging.getLogger("edu")

SCENE_CLASS = "GeneratedScene"
ALLOWED_IMPORTS = {"manim", "math", "random", "numpy", "colorsys", "itertools", "functools"}
FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
                   "globals", "locals", "vars", "setattr", "delattr"}
LATEX_NAMES = {"Tex", "MathTex", "Matrix", "IntegerMatrix", "DecimalMatrix", "MobjectMatrix",
               "Title", "BulletedList", "SingleStringMathTex"}


class SceneError(RuntimeError):
    """A scene could not be produced even after the repair attempts."""


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #
def split_scenes(text: str):
    """'Topic: X / Scene 1: ... / Scene 2: ...' -> (header, [scene descriptions]).
    No 'Scene N:' markers -> the whole text is ONE scene."""
    text = text.strip()
    parts = re.split(r"(?im)^[ \t]*scene[ \t]*\d+[ \t]*[:.\-\u2013)][ \t]*", text)
    head = parts[0].strip()
    scenes = [p.strip() for p in parts[1:] if p.strip()]
    if not scenes:
        return "", [text]
    return head, scenes


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def _rules(cfg: VideoConfig, seconds: float, allow_latex: bool) -> str:
    fw, fh = cfg.frame_size
    latex = ("LaTeX is available: Tex/MathTex are allowed." if allow_latex else
             "LaTeX is NOT installed. Never use Tex, MathTex, Matrix, Title or BulletedList. "
             "Use Text() (unicode symbols like ², √, →, ≈ are fine).")
    return f"""RULES (follow all):
- Output ONE complete Python file only. No explanation. No markdown outside a single ```python block.
- Start with `from manim import *`. Allowed imports: manim, math, random, numpy. Nothing else.
- Define exactly one class `{SCENE_CLASS}(Scene)` with `construct(self)`.
- The frame is {fw:.2f} wide x {fh:.2f} tall scene units, centered on the origin. Keep every object inside
  x in [{-fw / 2 + 0.4:.2f}, {fw / 2 - 0.4:.2f}], y in [{-fh / 2 + 0.4:.2f}, {fh / 2 - 0.4:.2f}].
  Never read or set `config.*`; use the numbers above.
  Scale or `.set_width()` anything that could overflow; arrange with `.arrange()`, `.next_to()`, `VGroup`.
- {latex}
- No files, no network, no images, no sound, no external assets. Do not touch `config`.
- Total animation time about {seconds:.0f} seconds (sum of run_time and wait calls). Finish with self.wait(0.5).
- Use only current Manim Community API (Create, Write, FadeIn, FadeOut, Transform, ReplacementTransform,
  GrowFromCenter, LaggedStart, Axes, NumberPlane, Arrow, Circle, Rectangle, RoundedRectangle, Line, Dot, VGroup...).
  NOT ShowCreation / TextMobject / OldTex.
- For code or terminal text use Text(..., font="Courier New") inside a RoundedRectangle.
- Choose visuals that match the MEANING of the scene (diagram, graph, process, timeline, shapes in motion...).
  Do not just show a paragraph of text. Keep text short and large enough to read on a phone.
- Clean up at the end of the scene is optional; no leftover errors."""


def _code_prompt(cfg, scenes, idx, header, style, seconds, allow_latex) -> str:
    n = len(scenes)
    overview = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(scenes))
    return f"""You are an expert Manim Community Edition (Python) animator.
Write the animation for scene {idx + 1} of {n} of a short explainer video.
{f'Video brief: {header}' if header else ''}
Visual style (keep consistent across scenes): {style}

All scenes (for context, only build scene {idx + 1}):
{overview}

SCENE {idx + 1} TO BUILD:
{scenes[idx]}

{_rules(cfg, seconds, allow_latex)}"""


def _repair_prompt(cfg, scene_text, code, error, seconds, allow_latex) -> str:
    return f"""This Manim Community Edition code failed. Fix it and return the COMPLETE corrected file.
Keep the same visual idea; change only what is needed to make it run and look right.

SCENE DESCRIPTION:
{scene_text}

ERROR:
{error[-2500:]}

CODE:
```python
{code}
```

{_rules(cfg, seconds, allow_latex)}"""


# --------------------------------------------------------------------------- #
# Validation of generated code
# --------------------------------------------------------------------------- #
def extract_code(text: str) -> str:
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else strip_fences(text)).strip()


def validate_code(code: str, allow_latex: bool = False) -> str:
    """Return '' when the code looks safe and well formed, else an error message for the repair prompt."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError: {e}"
    has_class = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    return f"Forbidden import: {a.name}. Allowed: {sorted(ALLOWED_IMPORTS)}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS or node.level:
                return f"Forbidden import: {node.module}. Allowed: {sorted(ALLOWED_IMPORTS)}"
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS:
                return f"Forbidden name: {node.id}"
            if node.id == "config":
                return "Do not use `config`; the frame size is fixed by the runner. Use the numbers given."
            if not allow_latex and node.id in LATEX_NAMES:
                return f"{node.id} needs LaTeX, which is not installed. Use Text() instead."
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr != "__init__":
                return f"Forbidden attribute: {node.attr}"
        elif isinstance(node, ast.ClassDef) and node.name == SCENE_CLASS:
            has_class = True
    if not has_class:
        return f"Missing class `{SCENE_CLASS}(Scene)`."
    return ""


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _prelude(cfg: VideoConfig) -> str:
    fw, fh = cfg.frame_size
    return f"from manim import *\nconfig.frame_width = {fw}\nconfig.frame_height = {fh}\n\n"


def render_scene(code: str, name: str, cfg: VideoConfig, work: Path, fps: int, timeout: int = 900):
    """Write the scene file, run `manim render` in a separate process. Returns (mp4_path | None, error_text)."""
    py = work / f"{name}.py"
    py.write_text(_prelude(cfg) + code + "\n", encoding="utf-8")
    media = work / "media"
    w, h = cfg.render_size
    cmd = [sys.executable, "-m", "manim", "render", "--resolution", f"{w},{h}", "--fps", str(fps),
           "--media_dir", str(media), "-o", name, "--format", "mp4", "--disable_caching",
           "--progress_bar", "none", "-v", "WARNING", str(py), SCENE_CLASS]
    # keep API keys away from the generated code's process
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    try:
        r = run(cmd, timeout=timeout, env=env, cwd=str(work))
    except subprocess.TimeoutExpired:
        return None, f"Render timed out after {timeout}s (animation too heavy or infinite loop)."
    except FileNotFoundError as e:
        return None, f"Could not start manim: {e}. Install it with: pip install manim"
    if r.returncode != 0:
        return None, ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-3000:]
    found = [p for p in media.rglob(f"{name}.mp4") if "partial_movie_files" not in p.parts]
    if not found:
        return None, "Manim finished but produced no mp4.\n" + ((r.stderr or "") + (r.stdout or ""))[-1500:]
    out = max(found, key=lambda p: p.stat().st_mtime)
    try:
        if duration(out) <= 0.2:
            return None, "Rendered video is empty (scene had no animation or waits)."
    except (ValueError, FileNotFoundError) as e:
        return None, f"Could not read rendered video: {e}"
    return out, ""


def concat_videos(parts, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        shutil.copy(parts[0], out)
        return out
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out)])
    lst.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg concat failed: {r.stderr[-800:]}")
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def make_gemini(cfg: VideoConfig) -> Gemini:
    load_env()
    keys = env_list("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    if not keys:
        raise SystemExit("No Gemini key found. Put GEMINI_API_KEY=... in a .env file next to run.py.")
    return Gemini(keys, light_models=csv(cfg.light_models), medium_models=csv(cfg.medium_models),
                  code_models=csv(cfg.code_models), rpm=cfg.rpm, max_wait=cfg.max_wait)


def build_scene(gem: Gemini, cfg, scenes, idx, header, style, seconds, work, fps, allow_latex) -> Path:
    """Generate -> validate -> render; on failure ask Gemini to repair (cfg.retries times)."""
    name = f"scene_{idx + 1:02d}"
    final = work / f"{name}.mp4"
    if final.exists():
        log.info("scene %d: (cached) %s", idx + 1, final.name)
        return final

    log.info("scene %d/%d: asking Gemini for code", idx + 1, len(scenes))
    code = extract_code(gem.code(_code_prompt(cfg, scenes, idx, header, style, seconds, allow_latex), task="code"))
    error = ""
    for attempt in range(cfg.retries + 1):
        if attempt:
            log.warning("scene %d: repair attempt %d/%d (%s)", idx + 1, attempt, cfg.retries,
                        error.strip().splitlines()[-1][:120] if error.strip() else "")
            code = extract_code(gem.code(
                _repair_prompt(cfg, scenes[idx], code, error, seconds, allow_latex), task="repair"))
        error = validate_code(code, allow_latex)
        if not error:
            log.info("scene %d: rendering (attempt %d)", idx + 1, attempt + 1)
            mp4, error = render_scene(code, name, cfg, work, fps)
            if mp4:
                shutil.copy(mp4, final)
                (work / f"{name}.final.py").write_text(_prelude(cfg) + code + "\n", encoding="utf-8")
                return final
    (work / f"{name}.failed.py").write_text(code, encoding="utf-8")
    raise SceneError(f"Scene {idx + 1} failed after {cfg.retries} repair attempts:\n{error[-800:]}")


def generate_video(prompt: str, *, aspect: str = "16:9", quality: str = "low", fps: int = 30,
                   scene_seconds: float = 10.0, style: str = "", retries: int = 2,
                   out_dir: str = "output", work_dir: str = "build", allow_latex: bool = False,
                   gemini: Gemini | None = None) -> Path:
    """Scene description(s) -> MP4. Returns the path of the final video.

    prompt  : free text, or 'Topic: ...' followed by 'Scene 1: ...', 'Scene 2: ...' lines.
    aspect  : '16:9' | '9:16' | '1:1' | '4:5'
    quality : 'low' (480p short side, fast on CPU) | 'medium' | 'high'
    """
    header, scenes = split_scenes(prompt)
    cfg = VideoConfig(topic=(header or scenes[0])[:80], aspect=aspect, quality=quality,
                      retries=retries, allow_latex=allow_latex)
    style = style or "dark background, clean flat shapes, bright accent colors, smooth easing"
    tag = hashlib.md5(f"{prompt}|{cfg.aspect}|{cfg.quality}|{fps}".encode()).hexdigest()[:10]
    work = Path(work_dir) / f"scene-video-{tag}"
    work.mkdir(parents=True, exist_ok=True)

    gem = gemini or make_gemini(cfg)
    parts = [build_scene(gem, cfg, scenes, i, header, style, scene_seconds, work, fps, allow_latex)
             for i in range(len(scenes))]
    out = concat_videos(parts, Path(out_dir) / f"video-{tag}.mp4")
    log.info("done: %s (%.1fs, %dx%d)", out, duration(out), *cfg.render_size)
    return out
