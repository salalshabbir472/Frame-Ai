"""
remotion_video.py - same idea as scene_video.py, but the renderer is Remotion (React/TypeScript).

    from remotion_video import generate_video
    path = generate_video("Scene 1: ...\nScene 2: ...")

You only run Python. This module:
    1. creates / reuses a small Remotion project  (build/remotion_project, `npm install` only the first time)
    2. asks Gemini to write a React + Remotion component (TSX) for every scene   <- real code, no templates
    3. validates it (import allow-list, forbidden APIs), then type-checks it with `tsc`
    4. renders it with `npx remotion render`
    5. on any error sends the error back to Gemini (limited repair attempts)
    6. checks the rendered file (duration / size) and joins the scenes with ffmpeg

Needs: Node.js 18+ (node, npm, npx on PATH), ffmpeg on PATH, and `pip install google-genai`.
       Manim is NOT needed for this engine.

SECURITY NOTE: Gemini's code is untrusted. It is (a) statically checked (no network APIs, no remote URLs,
no dynamic import/eval, only react / remotion packages), (b) built and rendered in separate processes with
API keys removed from their environment, (c) time limited. But the TSX finally runs inside headless Chromium
on your machine WITHOUT an OS-level sandbox, so this is NOT secure isolation. For real isolation run the
whole thing inside Docker or a VM.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from config import VideoConfig, duration, run, strip_fences
from gemini_client import Gemini
from scene_video import SceneError, concat_videos, make_gemini, split_scenes

log = logging.getLogger("edu")

ALLOWED_MODULES = {"react", "remotion", "@remotion/shapes", "@remotion/paths"}

PACKAGE_JSON = {
    "name": "frame-remotion-runner", "private": True, "version": "1.0.0",
    "dependencies": {
        "remotion": "^4.0.0", "@remotion/cli": "^4.0.0",
        "@remotion/shapes": "^4.0.0", "@remotion/paths": "^4.0.0",
        "react": "^18.3.1", "react-dom": "^18.3.1",
    },
    "devDependencies": {"typescript": "^5.4.0", "@types/react": "^18.3.0"},
}

TSCONFIG = {
    "compilerOptions": {
        "target": "ES2020", "module": "ESNext", "moduleResolution": "bundler", "jsx": "react-jsx",
        "strict": False, "noEmit": True, "skipLibCheck": True, "esModuleInterop": True,
        "lib": ["ES2020", "DOM"], "types": [],
    }
}

# (regex, message). Checked on the code with the standard SVG namespace URL removed.
FORBIDDEN = [
    (r"\brequire\s*\(", "require() is not allowed"),
    (r"\bimport\s*\(", "dynamic import() is not allowed"),
    (r"\beval\s*\(", "eval is not allowed"),
    (r"\bnew\s+Function\b|\bFunction\s*\(", "Function constructor is not allowed"),
    (r"\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|\bnew\s+Worker\b",
     "network / worker APIs are not allowed"),
    (r"\bprocess\.(env|exit|cwd|binding)|child_process|\bfs\b\.", "node APIs are not allowed"),
    (r"localStorage|sessionStorage|indexedDB|document\.cookie|document\.write", "browser storage APIs are not allowed"),
    (r"window\.open|window\.location|document\.location|\blocation\.(href|assign|replace)", "navigation is not allowed"),
    (r"dangerouslySetInnerHTML|<script|<iframe|<embed|<object", "raw HTML / script injection is not allowed"),
    (r"https?://", "remote URLs are not allowed (no external assets); draw everything with SVG/CSS"),
    (r"@import|url\(\s*['\"]?(?!#)", "CSS @import / url() are not allowed (use SVG ids like url(#grad) only)"),
    (r"\bstaticFile\b|<Img\b|<Audio\b|<Video\b|<OffthreadVideo\b|<Html5", "media / asset components are not allowed"),
]
_SVG_NS = "http://www.w3.org/2000/svg"


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def _rules(w, h, fps, frames, seconds, tag="TSX") -> str:
    return f"""RULES (follow all):
- Output ONE complete TypeScript React file only, inside a single ```tsx block. No explanation.
- Imports allowed ONLY from: "react", "remotion", "@remotion/shapes", "@remotion/paths". Nothing else, no relative imports.
- `export default` a React component named Scene (no required props). It is rendered as a Remotion composition of
  exactly {w}x{h} px, {fps} fps, {frames} frames ({seconds:.0f} s). The animation must fit in those frames.
- Animate ONLY with Remotion's frame system: useCurrentFrame(), useVideoConfig(), interpolate(), spring(),
  Sequence, AbsoluteFill, Easing. NEVER use CSS transitions/animations, setTimeout, requestAnimationFrame,
  useState/useEffect-driven animation, or Math.random (use `random("seed")` from "remotion").
- Compute sizes from useVideoConfig() (width, height) so the layout works in any aspect ratio
  (tall frames: stack things vertically; wide frames: lay out horizontally). Keep everything inside the frame
  with at least 5% margin. Text must be large enough to read on a phone (>= {max(int(min(w, h) * 0.04), 18)} px).
- No images, no video, no audio, no web fonts, no remote URLs, no CSS url(). Draw with inline <svg>, divs, CSS
  gradients, shapes. Font: fontFamily "Inter, Segoe UI, Arial, sans-serif".
- No fetch / network / storage / eval / dynamic import / Node APIs.
- Choose visuals that match the MEANING of the scene (diagram, flow, graph, timeline, stacking, nesting, motion);
  do not just show a paragraph of text. Make it well-composed, with smooth easing and clear stagger.
- The file must type-check with TypeScript (React.FC, typed props where needed). Always end with the last
  element visible and settled for the final ~0.5 s."""


def _code_prompt(scenes, idx, header, style, w, h, fps, frames, seconds) -> str:
    n = len(scenes)
    overview = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(scenes))
    return f"""You are an expert Remotion (React + TypeScript) motion-graphics developer.
Write the animation component for scene {idx + 1} of {n} of a short explainer video.
{f'Video brief: {header}' if header else ''}
Visual style (keep consistent across scenes): {style}

All scenes (context only, build scene {idx + 1}):
{overview}

SCENE {idx + 1} TO BUILD:
{scenes[idx]}

{_rules(w, h, fps, frames, seconds)}"""


def _repair_prompt(scene_text, code, error, w, h, fps, frames, seconds) -> str:
    return f"""This Remotion TSX scene failed. Fix it and return the COMPLETE corrected file.
Keep the same visual idea; change only what is needed.

SCENE DESCRIPTION:
{scene_text}

ERROR:
{error[-2500:]}

CODE:
```tsx
{code}
```

{_rules(w, h, fps, frames, seconds)}"""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def extract_code(text: str) -> str:
    m = re.search(r"```(?:tsx|typescript|ts|jsx|javascript|js)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else strip_fences(text)).strip()


def validate_tsx(code: str) -> str:
    """'' when OK, else an error message that is fed to the repair prompt."""
    if not code.strip():
        return "Empty file."
    scan = code.replace(_SVG_NS, "")
    mods = set(re.findall(r"\bfrom\s+['\"]([^'\"]+)['\"]", scan)) | \
        set(re.findall(r"^\s*import\s+['\"]([^'\"]+)['\"]", scan, re.M))
    for m in sorted(mods):
        if m not in ALLOWED_MODULES:
            return f'Forbidden import "{m}". Allowed: {sorted(ALLOWED_MODULES)}'
    for pat, msg in FORBIDDEN:
        hit = re.search(pat, scan)
        if hit:
            return f"{msg} (found: {hit.group(0)!r})"
    if not re.search(r"export\s+default", code):
        return "Missing `export default` of the Scene component."
    if "useCurrentFrame" not in code:
        return "The scene must animate with useCurrentFrame() (Remotion frame-based animation)."
    return ""


# --------------------------------------------------------------------------- #
# Node / Remotion project
# --------------------------------------------------------------------------- #
def _clean_env():
    return {k: v for k, v in os.environ.items()
            if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}


def find_node():
    node, npm, npx = shutil.which("node"), shutil.which("npm"), shutil.which("npx")
    if not (node and npm and npx):
        raise SystemExit("Node.js 18+ is required for the Remotion engine (node/npm/npx not on PATH). "
                         "Install it from https://nodejs.org or use --engine manim.")
    v = run([node, "--version"]).stdout.strip()
    try:
        if int(v.lstrip("v").split(".")[0]) < 18:
            raise SystemExit(f"Node {v} is too old; Remotion needs Node 18+.")
    except ValueError:
        pass
    return npm, npx


def ensure_project(root: Path, npm: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in (("package.json", PACKAGE_JSON), ("tsconfig.json", TSCONFIG)):
        if not (root / name).exists():
            (root / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
    if not (root / "node_modules" / "remotion").exists():
        log.info("installing Remotion (first run only, a few minutes) ...")
        r = run([npm, "install", "--no-audit", "--no-fund"], cwd=str(root), timeout=1800)
        if r.returncode != 0:
            raise RuntimeError("npm install failed:\n" + (r.stderr or r.stdout)[-1500:])


def _write_scene_files(project: Path, tag: str, idx: int, code: str, w, h, fps, frames):
    d = project / "src" / tag
    d.mkdir(parents=True, exist_ok=True)
    n = f"{idx + 1:02d}"
    (d / f"scene_{n}.tsx").write_text(code + "\n", encoding="utf-8")
    (d / f"entry_{n}.tsx").write_text(f"""import React from 'react';
import {{registerRoot, Composition}} from 'remotion';
import Scene from './scene_{n}';

const Root: React.FC = () => (
  <Composition id="Scene" component={{Scene}} durationInFrames={{{frames}}} fps={{{fps}}} width={{{w}}} height={{{h}}} />
);
registerRoot(Root);
""", encoding="utf-8")
    (d / f"tsconfig.scene_{n}.json").write_text(json.dumps(
        {"extends": "../../tsconfig.json", "include": [f"entry_{n}.tsx", f"scene_{n}.tsx"]}), encoding="utf-8")
    return d, n


def video_info(path: Path):
    """(width, height, seconds) of the first video stream, or None when ffprobe is missing / cannot read it."""
    try:
        r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height:format=duration", "-of", "json", str(path)])
        data = json.loads(r.stdout)
        st = data["streams"][0]
        return int(st["width"]), int(st["height"]), float(data["format"]["duration"])
    except (ValueError, KeyError, IndexError, FileNotFoundError, OSError) as e:
        log.warning("  could not inspect %s with ffprobe (%s); skipping the size/duration check", path.name, e)
        return None


def render_scene(code, idx, tag, cfg, fps, seconds, project: Path, npx: str, out: Path, timeout=1200):
    """type-check + render one scene. Returns (mp4 | None, error_text)."""
    project = Path(project).resolve()
    w, h = cfg.render_size
    frames = max(int(round(seconds * fps)), fps)
    d, n = _write_scene_files(project, tag, idx, code, w, h, fps, frames)
    env = _clean_env()
    try:
        r = run([npx, "--no-install", "tsc", "-p", str(d / f"tsconfig.scene_{n}.json"), "--noEmit"],
                cwd=str(project), env=env, timeout=300)
        if r.returncode != 0:
            return None, "TypeScript errors:\n" + (r.stdout + r.stderr).strip()[-2500:]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        r = run([npx, "--no-install", "remotion", "render", f"src/{tag}/entry_{n}.tsx", "Scene", str(out.resolve()),
                 "--codec=h264", f"--concurrency={max(1, min(2, os.cpu_count() or 1))}",
                 "--log=error", "--overwrite"],
                cwd=str(project), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"Timed out after {timeout}s (animation too heavy)."
    if r.returncode != 0 or not out.exists():
        return None, "Remotion render failed:\n" + ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-2500:]
    info = video_info(out)
    if info:                      # None = ffprobe unavailable: the render itself succeeded, so accept it
        vw, vh, vd = info
        if (vw, vh) != (w, h):
            return None, f"Rendered size {vw}x{vh} != expected {w}x{h}"
        if vd < seconds * 0.8:
            return None, f"Rendered duration {vd:.1f}s is shorter than expected {seconds:.1f}s"
    return out, ""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_scene(gem: Gemini, cfg, scenes, idx, header, style, seconds, fps, tag, project, npx, work) -> Path:
    name = f"scene_{idx + 1:02d}"
    final = work / f"{name}.mp4"
    if final.exists():
        log.info("scene %d: (cached) %s", idx + 1, final.name)
        return final
    w, h = cfg.render_size
    frames = max(int(round(seconds * fps)), fps)

    log.info("scene %d/%d: asking Gemini for Remotion code", idx + 1, len(scenes))
    code = extract_code(gem.code(_code_prompt(scenes, idx, header, style, w, h, fps, frames, seconds), task="code"))
    error = ""
    for attempt in range(cfg.retries + 1):
        if attempt:
            log.warning("scene %d: repair attempt %d/%d (%s)", idx + 1, attempt, cfg.retries,
                        error.strip().splitlines()[-1][:120] if error.strip() else "")
            code = extract_code(gem.code(
                _repair_prompt(scenes[idx], code, error, w, h, fps, frames, seconds), task="repair"))
        error = validate_tsx(code)
        if not error:
            log.info("scene %d: type-check + render (attempt %d)", idx + 1, attempt + 1)
            partial = work / f"{name}.partial.mp4"
            mp4, error = render_scene(code, idx, tag, cfg, fps, seconds, project, npx, partial)
            if mp4:
                os.replace(partial, final)       # only a verified render gets the real name (= cache)
                (work / f"{name}.final.tsx").write_text(code + "\n", encoding="utf-8")
                return final
            partial.unlink(missing_ok=True)
    (work / f"{name}.failed.tsx").write_text(code + "\n", encoding="utf-8")
    raise SceneError(f"Scene {idx + 1} failed after {cfg.retries} repair attempts:\n{error[-800:]}")


def generate_video(prompt: str, *, aspect: str = "16:9", quality: str = "low", fps: int = 30,
                   scene_seconds: float = 10.0, style: str = "", retries: int = 2,
                   out_dir: str = "output", work_dir: str = "build", gemini: Gemini | None = None,
                   **_ignored) -> Path:
    """Scene description(s) -> MP4 using Remotion. Returns the final video path."""
    header, scenes = split_scenes(prompt)
    cfg = VideoConfig(topic=(header or scenes[0])[:80], aspect=aspect, quality=quality, retries=retries)
    style = style or "dark gradient background, clean flat shapes, bright accent colors, smooth easing"
    tag = hashlib.md5(f"{prompt}|{cfg.aspect}|{cfg.quality}|{fps}|remotion".encode()).hexdigest()[:10]
    work = Path(work_dir) / f"remotion-video-{tag}"
    work.mkdir(parents=True, exist_ok=True)

    npm, npx = find_node()
    project = Path(work_dir) / "remotion_project"
    ensure_project(project, npm)

    gem = gemini or make_gemini(cfg)
    parts = [build_scene(gem, cfg, scenes, i, header, style, scene_seconds, fps, tag, project, npx, work)
             for i in range(len(scenes))]
    out = concat_videos(parts, Path(out_dir) / f"video-{tag}.mp4")
    log.info("done: %s (%.1fs, %dx%d)", out, duration(out), *cfg.render_size)
    return out
