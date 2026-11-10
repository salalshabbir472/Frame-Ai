"""
checkpoint.py - a video that stopped half-way is zipped, stored, and continued later from the same point.

Why: a GitHub Actions runner is thrown away at the end of every run. Without this, a video that stopped
(Gemini quota wall, 6 h time limit, crash, cancelled run) lost its rendered scenes and narration audio and
started again from zero. Now `build/<slug>/` (script, audio, every finished scene clip, the json records that
say which stage is done) is packed into ONE zip. The next run unpacks it and the pipeline's own per-stage /
per-scene cache does the rest: finished work is skipped, only the missing part is built.

Safe against half-written files
  * a scene clip is only listed in 5_clips.json AFTER its render finished, audio only in 2_audio.json after
    its wav is complete, and every json is written atomically. So a zip taken at ANY moment is consistent:
    an unfinished scene is simply built again.
  * json files are put in the zip AFTER the media files, so a record never points at a file that is missing.

Used by drive_runner.py (zip goes to <playlist>/Checkpoints/<slug>.checkpoint.zip in Drive). Also usable by hand:

    python checkpoint.py pack   build/<slug> my.zip
    python checkpoint.py unpack my.zip build/<slug>
    python checkpoint.py info   my.zip
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

log = logging.getLogger("edu")

META = "_checkpoint.json"
SKIP_DIRS = {"node_modules", "__pycache__", ".git"}          # big and rebuilt by itself
SKIP_SUFFIX = (".tmp", ".part", ".partial")                  # half-written files
ALREADY_PACKED = {".mp4", ".mov", ".webm", ".mp3", ".m4a", ".png", ".jpg", ".jpeg", ".webp", ".zip"}


def _files(work: Path):
    out = []
    for p in work.rglob("*"):
        if not p.is_file() or p.name.endswith(SKIP_SUFFIX):
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(work).parts):
            continue
        out.append(p)
    # media first, json last (see "Safe against half-written files")
    return sorted(out, key=lambda p: (p.suffix.lower() == ".json", str(p)))


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def progress(work: Path) -> str:
    """One short line about how far the video is, e.g. 'script ok, audio 12/12, scenes 5/12'."""
    work = Path(work)
    script = _load(work / "1_script.json") or {}
    total = len(script.get("scenes") or [])
    audio = _load(work / "2_audio.json") or {}
    clips = _load(work / "5_clips.json") or {}
    clips_ok = sum(1 for v in clips.values() if Path(v).exists())
    parts = []
    if script:
        parts.append("script ok")
    if total:
        parts.append(f"audio {len(audio)}/{total}")
        parts.append(f"scenes {clips_ok}/{total}")
    return ", ".join(parts) or "just started"


def pack(work: Path, zip_path: Path) -> Path:
    """Zip a work folder (build/<slug>) into zip_path. Returns zip_path."""
    work, zip_path = Path(work), Path(zip_path)
    if not work.is_dir():
        raise FileNotFoundError(f"nothing to checkpoint: {work} does not exist")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    meta = {"saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC",
            "work": str(work.resolve()), "cwd": str(Path.cwd().resolve()), "progress": progress(work)}
    with zipfile.ZipFile(tmp, "w") as zf:
        for p in _files(work):
            kind = zipfile.ZIP_STORED if p.suffix.lower() in ALREADY_PACKED else zipfile.ZIP_DEFLATED
            try:
                zf.write(p, p.relative_to(work).as_posix(), compress_type=kind)
            except FileNotFoundError:        # a temp file vanished while we were zipping
                continue
        zf.writestr(META, json.dumps(meta, indent=2))
    os.replace(tmp, zip_path)
    log.info("checkpoint packed: %s (%.1f MB) - %s", zip_path.name, zip_path.stat().st_size / 1e6, meta["progress"])
    return zip_path


def info(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read(META).decode("utf-8"))


def unpack(zip_path: Path, work: Path) -> str:
    """Unpack a checkpoint into the work folder and repair absolute paths saved in the json records.
    Returns the progress line. A broken zip raises and leaves `work` untouched."""
    zip_path, work = Path(zip_path), Path(work)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise zipfile.BadZipFile(f"corrupt member {bad}")
        meta = json.loads(zf.read(META).decode("utf-8")) if META in zf.namelist() else {}
        work.mkdir(parents=True, exist_ok=True)
        root = work.resolve()
        for name in zf.namelist():
            if name == META or name.endswith("/"):
                continue
            target = (root / name).resolve()
            if root != target and root not in target.parents:      # path traversal guard
                raise zipfile.BadZipFile(f"unsafe path in zip: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    # 5_clips.json / 2_audio.json store absolute paths: another runner may live in another folder
    old_cwd, new_cwd = meta.get("cwd", ""), str(Path.cwd().resolve())
    if old_cwd and old_cwd != new_cwd:
        for js in work.rglob("*.json"):
            try:
                txt = js.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            if old_cwd in txt:
                js.write_text(txt.replace(old_cwd, new_cwd), encoding="utf-8")
    line = meta.get("progress") or progress(work)
    log.info("checkpoint restored into %s (saved %s): %s", work, meta.get("saved_at", "?"), line)
    return line


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == "pack":
        pack(Path(a[1]), Path(a[2]))
    elif len(a) == 3 and a[0] == "unpack":
        unpack(Path(a[1]), Path(a[2]))
    elif len(a) == 2 and a[0] == "info":
        print(json.dumps(info(Path(a[1])), indent=2))
    else:
        sys.exit(__doc__)
