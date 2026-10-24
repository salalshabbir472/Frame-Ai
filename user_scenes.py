"""
user_scenes.py - render the scenes the user wrote THEMSELVES (Manim .py, Remotion .tsx / .jsx / .ts / .js / .mjs / .zip).

"Used as it is": Frame never edits the user's code. It only runs it, in a separate process with the API keys / tokens
removed from its environment, with a time limit, and then fits the clip to the narration like every other clip
(media.sync_scene holds the last frame if the animation is shorter than the voice, and letterboxes it with a blurred
copy of itself if its shape differs from the video's shape).

  Manim    : the first Scene class that has a construct() is rendered with the video's quality. The file keeps its own frame.
  Remotion : * a file that exports a component (export default, or a PascalCase export) is wrapped into a composition that has
               the video's size and the exact length of the narration, so it simply fills the scene;
             * a file / project that registers its own root (registerRoot + <Composition>) is rendered as it is, with its own
               size, fps and length;
             * a .zip is unpacked safely (no ../, no links, size limits). With a package.json it gets its own `npm install`
               (--ignore-scripts); without one it uses the shared Remotion project of this machine.
  Extra @remotion/* packages a single file imports are installed on demand (same version as Remotion itself).

Any problem raises UserSceneError; the caller then builds the scene from its description (or a title card) instead and says so in
the scene report. A scene never kills the video.

SECURITY NOTE: this is the user's OWN code from their OWN Drive, so it is not run through the strict allow-list that
Gemini's code goes through. It still runs without secrets and with a timeout, but it is NOT sandboxed: use Docker / a VM if the
repo's owner and the author of the scene files are different people.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

from config import QUALITY_FLAGS, run
from media import render_manim
from remotion_video import _clean_env, video_info

log = logging.getLogger("edu")

FPS = 30
MAX_FILE = 2_000_000            # one source file
MAX_ZIP_FILES = 5000
MAX_ZIP_BYTES = 250_000_000     # unpacked
SRC_EXT = (".tsx", ".ts", ".jsx", ".js", ".mjs")
SKIP_DIRS = {"node_modules", ".git", "out", "build", "dist", ".next", "__MACOSX"}
SCENE_BASES = ("Scene", "MovingCameraScene", "ThreeDScene", "ZoomedScene", "VectorScene", "LinearTransformationScene")


class UserSceneError(RuntimeError):
    """The user's own scene file could not be rendered."""


def own_engines(cfg) -> set:
    return {c["engine"] for c in cfg.custom_scenes if c.get("source") == "file" and c.get("engine") and c.get("local_path")}


def own_scene(cfg, plan):
    """The user's scene (with its local file) that a script scene refers to, or None."""
    if not plan or plan.get("mode") != "file":
        return None
    c = next((x for x in cfg.custom_scenes if x.get("order") == plan.get("ref")), None)
    return c if c and c.get("local_path") and Path(c["local_path"]).exists() else None


# --------------------------------------------------------------------------- #
# Manim
# --------------------------------------------------------------------------- #
def pick_scene_class(code: str) -> str:
    """Name of the class to render: the first one that looks like a Manim scene and defines construct()."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise UserSceneError(f"the Manim file has a syntax error: {e}") from e
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and any(
        isinstance(f, ast.FunctionDef) and f.name == "construct" for f in n.body)]

    def base_names(n):
        return [getattr(b, "id", getattr(b, "attr", "")) for b in n.bases]
    for n in classes:
        if any(b in SCENE_BASES or b.endswith("Scene") for b in base_names(n)):
            return n.name
    if classes:
        return classes[0].name
    raise UserSceneError("no Manim scene class with a construct() method found in the file")


def render_own_manim(cfg, scene: dict, sid: int, work: Path):
    src = Path(scene["local_path"])
    if src.stat().st_size > MAX_FILE:
        raise UserSceneError("the Manim file is larger than 2 MB")
    code = src.read_text(encoding="utf-8", errors="replace")
    cls = pick_scene_class(code)
    d = (work / "own" / f"scene_{sid:02d}").resolve()
    d.mkdir(parents=True, exist_ok=True)
    py = d / f"own_{sid:02d}.py"
    py.write_text(code, encoding="utf-8")
    if not shutil.which("manim"):
        raise UserSceneError("Manim is not installed on this machine")
    video, err = render_manim(py, cls, (work / "media_own").resolve(), cfg.quality, env=_clean_env(), cwd=d, timeout=1800)
    if not video:
        raise UserSceneError("Manim could not render your file:\n" + err.strip()[-1200:])
    return video


# --------------------------------------------------------------------------- #
# Remotion
# --------------------------------------------------------------------------- #
def safe_unzip(zpath: Path, dest: Path):
    """Unpack a zip without ever writing outside `dest` (no absolute paths, no .., no links, size limits)."""
    dest = dest.resolve()
    try:
        z = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile as e:
        raise UserSceneError("the .zip file is damaged") from e
    with z:
        infos = z.infolist()
        if len(infos) > MAX_ZIP_FILES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise UserSceneError("the .zip is too big (limits: 5000 files, 250 MB unpacked)")
        for i in infos:
            name = i.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or SKIP_DIRS & set(parts):
                continue
            if name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
                raise UserSceneError(f"unsafe path in the .zip: {i.filename}")
            if stat.S_ISLNK(i.external_attr >> 16):
                continue                                              # links are never unpacked
            target = (dest / Path(*parts)).resolve()
            if dest not in target.parents and target != dest:
                raise UserSceneError(f"unsafe path in the .zip: {i.filename}")
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(i))
    kids = [p for p in dest.iterdir()]
    return kids[0] if len(kids) == 1 and kids[0].is_dir() else dest        # a single top folder is the project root


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.stat().st_size <= 600_000 else ""


def _sources(root: Path):
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in SRC_EXT and not (SKIP_DIRS & set(p.relative_to(root).parts)):
            out.append(p)
        if len(out) > 400:
            break
    return out


_COMP = re.compile(r"<Composition\b[\s\S]{0,700}?\bid\s*=\s*\{?\s*[\"'`]([A-Za-z0-9-]+)[\"'`]")


def find_entry(root: Path):
    """-> (entry file | None, composition id | None, component file | None).
    entry = a file with registerRoot(); component = a file with a component to wrap when there is no entry."""
    files = _sources(root)
    texts = {p: _read(p) for p in files}
    reg = [p for p in files if "registerRoot(" in texts[p]]
    pkg = root / "package.json"
    want_entry = want_comp = None
    if pkg.exists():
        try:
            scripts = " ".join(str(v) for v in (json.loads(_read(pkg)).get("scripts") or {}).values())
        except ValueError:
            scripts = ""
        m = re.search(r"remotion\s+(?:render|still|studio|preview|bundle)\s+(?:--\S+\s+)*([\w./-]+\.(?:tsx?|jsx?|mjs))(?:\s+([A-Za-z0-9-]+))?", scripts)
        if m and (root / m.group(1)).exists():
            want_entry, want_comp = root / m.group(1), m.group(2)
    entry = want_entry or next((p for p in reg if p.stem == "index"), None) or (reg[0] if reg else None)
    if entry:
        comp = want_comp if want_comp and not want_comp.startswith("-") else None
        if not comp:
            ordered = sorted(files, key=lambda p: (p != entry, p.stem not in ("Root", "RemotionRoot", "Video"), str(p)))
            for p in ordered:
                m = _COMP.search(texts[p])
                if m:
                    comp = m.group(1)
                    break
        return entry, comp, None
    pref = {"scene": 0, "main": 1, "index": 2, "app": 3, "video": 4, "composition": 5}
    comps = [p for p in files if re.search(r"export\s+default|export\s+(?:const|function)\s+[A-Z]", texts[p])
             and not p.name.endswith(".d.ts")]
    comps.sort(key=lambda p: (pref.get(p.stem.lower(), 9), len(p.parts)))
    return None, None, (comps[0] if comps else None)


def _export_name(code: str):
    """('default', None) | ('named', 'Name') | None - how a component file exports its component."""
    if re.search(r"export\s+default", code):
        return "default", None
    m = re.search(r"export\s+(?:const|function)\s+(Scene)\b", code) or re.search(r"export\s+(?:const|function)\s+([A-Z]\w*)", code)
    return ("named", m.group(1)) if m else None


def ensure_remotion_packages(project: Path, npm: str, code: str):
    """@remotion/* packages the file imports but the shared project lacks are installed (same version as remotion)."""
    need = {m for m in re.findall(r"from\s+['\"](@remotion/[\w-]+)['\"]", code)
            if not (project / "node_modules" / m).exists()}
    if not need:
        return
    try:
        ver = json.loads((project / "node_modules" / "remotion" / "package.json").read_text())["version"]
    except (OSError, ValueError, KeyError) as e:
        raise UserSceneError("the shared Remotion project is not installed") from e
    log.info("  installing %s for your scene", ", ".join(sorted(need)))
    r = run([npm, "install", "--no-audit", "--no-fund", "--ignore-scripts", *[f"{m}@{ver}" for m in sorted(need)]],
            cwd=str(project), timeout=900, env=_clean_env())
    if r.returncode != 0:
        raise UserSceneError("could not install " + ", ".join(sorted(need)) + ":\n" + (r.stderr or r.stdout)[-600:])


def _wrapper(import_line: str, w: int, h: int, frames: int) -> str:
    return f"""import React from 'react';
import {{registerRoot, Composition}} from 'remotion';
{import_line}

const Root: React.FC = () => (
  <Composition id="Scene" component={{Scene as React.FC}} durationInFrames={{{frames}}} fps={{{FPS}}} width={{{w}}} height={{{h}}} />
);
registerRoot(Root);
"""


def _render(npx, cwd: Path, entry: Path, comp: str, out: Path, extra=(), timeout=1800):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    cmd = [npx, "--no-install", "remotion", "render", str(Path(entry).resolve()), comp, str(out.resolve()), "--codec=h264",
           f"--concurrency={max(1, min(2, os.cpu_count() or 1))}", "--log=error", "--overwrite", *extra]
    chrome = os.environ.get("REMOTION_BROWSER_EXECUTABLE")
    if chrome:
        cmd.append(f"--browser-executable={chrome}")
    try:
        r = run(cmd, cwd=str(cwd), env=_clean_env(), timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise UserSceneError(f"your Remotion scene took longer than {timeout}s to render") from e
    if r.returncode != 0 or not out.exists():
        raise UserSceneError("Remotion could not render your scene:\n" + ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-1500:])
    return out


def render_own_remotion(cfg, scene: dict, sid: int, seconds: float, work: Path, env):
    src = Path(scene["local_path"])
    if not getattr(env, "project", None) or not env.can.get("remotion"):
        raise UserSceneError("Remotion is not available on this machine")
    project, npx = Path(env.project).resolve(), env.npx
    npm = shutil.which("npm") or "npm"
    w, h = cfg.render_size
    frames = max(int(round(seconds * FPS)), FPS)
    out = work / "remotion" / f"own_scene_{sid:02d}.mp4"
    tmp = work / "own" / f"scene_{sid:02d}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    cwd = project                       # where `npx remotion` runs: the shared project, or the zip's own project

    if src.suffix.lower() == ".zip":
        root = safe_unzip(src, tmp / "zip")
        if (root / "package.json").exists():                           # a real project: its own dependencies
            log.info("  scene %s: npm install for your Remotion project", sid)
            r = run([npm, "install", "--no-audit", "--no-fund", "--ignore-scripts"], cwd=str(root), timeout=1800, env=_clean_env())
            if r.returncode != 0:
                raise UserSceneError("npm install failed for your project:\n" + (r.stderr or r.stdout)[-800:])
            home, cwd = root, root
        else:                                                         # loose files: use the shared project's packages
            home = project / "src" / f"{cfg.slug}-own" / f"own_{sid:02d}"
            shutil.rmtree(home, ignore_errors=True)
            shutil.copytree(root, home)
            root = home
        entry, comp, component = find_entry(root)
        if entry:
            if not comp:
                raise UserSceneError("no <Composition id=...> found in your project")
            log.info("  scene %s: rendering your project as it is (%s / %s)", sid, entry.name, comp)
            return _render(npx, cwd, entry, comp, out)
        if component is None:
            raise UserSceneError("no Remotion root (registerRoot) and no component to render found in the .zip")
        code = _read(component)
        base = home
    else:
        if src.stat().st_size > MAX_FILE:
            raise UserSceneError("the Remotion file is larger than 2 MB")
        if src.suffix.lower() not in SRC_EXT:
            raise UserSceneError(f"{src.suffix} is not a Remotion file type")
        home = project / "src" / f"{cfg.slug}-own" / f"own_{sid:02d}"
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True)
        ext = ".js" if src.suffix.lower() == ".mjs" else src.suffix.lower()
        component = home / f"user_scene{ext}"
        shutil.copyfile(src, component)
        code = _read(component)
        base = home
        if "registerRoot(" in code:                                    # a complete entry file: as it is
            comp = (_COMP.search(code) or [None, None])[1]
            if not comp:
                raise UserSceneError("your file calls registerRoot but has no <Composition id=...>")
            ensure_remotion_packages(project, npm, code)
            return _render(npx, cwd, component, comp, out)

    kind = _export_name(code)
    if not kind:
        raise UserSceneError("your Remotion file exports no component (use `export default` or `export const Scene`)")
    ensure_remotion_packages(project, npm, code)
    mod = "./" + component.relative_to(base).with_suffix("").as_posix()
    imp = f"import Scene from '{mod}';" if kind[0] == "default" else f"import {{ {kind[1]} as Scene }} from '{mod}';"
    entry = base / "entry_own.tsx"
    entry.write_text(_wrapper(imp, w, h, frames), encoding="utf-8")
    log.info("  scene %s: rendering your component at %dx%d for %.1fs", sid, w, h, seconds)
    return _render(npx, cwd, entry, "Scene", out)


# --------------------------------------------------------------------------- #
# Entry point used by remotion_stage.stage_visuals
# --------------------------------------------------------------------------- #
def render_user_scene(cfg, scene: dict, sp: dict, work: Path, env) -> tuple:
    """-> (mp4 path, "own-manim" | "own-remotion"). Raises UserSceneError."""
    sid, engine = sp["id"], scene.get("engine") or ""
    if engine == "manim":
        mp4 = render_own_manim(cfg, scene, sid, work)
    elif engine == "remotion":
        mp4 = render_own_remotion(cfg, scene, sid, float(sp["duration_sec"]), work, env)
    else:
        raise UserSceneError("cannot tell from the file name whether it is Manim (.py) or Remotion (.tsx .jsx .ts .js .zip)")
    info = video_info(Path(mp4))
    if info and info[2] < 0.3:
        raise UserSceneError("your scene rendered an empty video")
    return Path(mp4).resolve(), f"own-{engine}"
