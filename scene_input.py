"""
scene_input.py - the scenes the user storyboarded in the frontend, read into ONE clean shape. (pure Python, no Gemini)

Where they come from
  * the sheet cell "Scenes (JSON)"  : the frontend's own JSON (camelCase: mode / fileId / name), OR plain text somebody
                                      typed straight into the cell ("Scene 1: ...", numbered lines, blank-line blocks)
  * config.custom_scenes            : the snake_case list the frontend sends in frame_jobs.json (source / file_id / file_name)
  * the CLI                         : --scenes-file scenes.txt  or a JSON job with {"custom_scenes": [...]}

What comes out (one dict per scene, numbered 1..n in the order the user arranged them)
    {"order": 1, "title": "", "description": "", "source": "description" | "file",
     "engine": "" | "manim" | "remotion", "file_id": "", "url": "", "file_name": "", "local_path": ""}
  source = "description" -> Frame builds the scene from the text
  source = "file"        -> the user's own Manim (.py) / Remotion (.tsx .jsx .ts .js .mjs .zip) file is used as it is
  local_path             -> where the backend put the downloaded file (the runner sets it; the CLI may give it as "path")
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MAX_SCENES = 200
MAX_TEXT = 4000
ENGINES = ("manim", "remotion")

# "Scene 1: title" / "Scene 2 - title" / "Scene: title" / "Shot 3" / Urdu "سین 4" headers (same rules as the frontend parser)
SCENE_HEAD = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*{1,2}\s*)?(?:scene|seen|sequence|shot|منظر|سین)\s*#?\s*"
                        r"(?:\d+\s*[:.\-–—)]*|[:\-–—])\s*\**\s*(.*)$", re.I)
NUM_HEAD = re.compile(r"^\s*\d{1,3}\s*[.)]\s+(.*)$")


def detect_engine(name: str) -> str:
    n = (name or "").lower()
    if n.endswith(".py"):
        return "manim"
    if n.endswith((".tsx", ".jsx", ".ts", ".js", ".mjs", ".zip")):
        return "remotion"
    return ""


def parse_scene_text(text) -> list:
    """Plain text -> [{"title","description"}]. Headers first, then "1. text" lines, then blank-line blocks, then one line each."""
    t = str(text or "").replace("\r", "").strip()
    if not t:
        return []
    lines = t.split("\n")

    def split(rx):
        out, cur = [], None
        for ln in lines:
            m = rx.match(ln)
            if m:
                cur = {"head": re.sub(r"\*+\s*$", "", m.group(1) or "").strip(), "body": []}
                out.append(cur)
            elif cur is not None:
                cur["body"].append(ln)
        return out

    found = split(SCENE_HEAD)
    if not found:
        n = split(NUM_HEAD)
        if len(n) >= 2:
            found = n
    if found:
        out = []
        for f in found:
            body = "\n".join(f["body"]).strip()
            if not body:
                out.append({"title": "", "description": f["head"]})          # "Scene 3: a circle grows" -> the text is the description
            elif len(f["head"]) > 100:
                out.append({"title": "", "description": f["head"] + "\n" + body})
            else:
                out.append({"title": f["head"], "description": body})
        return [s for s in out if s["title"] or s["description"]][:MAX_SCENES]
    blocks = [b.strip() for b in re.split(r"\n\s*\n+", t) if b.strip()]
    if len(blocks) == 1 and len([l for l in lines if l.strip()]) > 1:
        blocks = [l.strip() for l in lines if l.strip()]
    return [{"title": "", "description": b} for b in blocks][:MAX_SCENES]


def _clean(c: dict) -> dict:
    file_id = str(c.get("file_id") or c.get("fileId") or "").strip()
    url = str(c.get("url") or "").strip()
    local = str(c.get("local_path") or c.get("path") or c.get("file_path") or "").strip()
    name = str(c.get("file_name") or c.get("name") or (Path(local).name if local else "")).strip()[:160]
    wants_file = c.get("source") == "file" or c.get("mode") == "file"          # backend shape | frontend shape
    is_file = bool(wants_file and (file_id or url or local))
    engine = str(c.get("engine") or "").strip().lower()
    if is_file and engine not in ENGINES:
        engine = detect_engine(name or local or url)
    return {"order": 0, "title": str(c.get("title") or "").strip()[:120],
            "description": str(c.get("description") or "").strip()[:MAX_TEXT],
            "source": "file" if is_file else "description", "engine": engine if is_file else "",
            "file_id": file_id, "url": url, "file_name": name if is_file else "", "local_path": local if is_file else ""}


def load_scenes(raw) -> list:
    """Anything the frontend / sheet / CLI may hand over -> the clean list described above. Never raises."""
    items = []
    if isinstance(raw, (list, tuple)):
        items = [x for x in raw if isinstance(x, dict)]
    else:
        s = str(raw or "").strip()
        if s:
            try:
                a = json.loads(s)
                items = [x for x in a if isinstance(x, dict)] if isinstance(a, list) else []
            except ValueError:
                items = parse_scene_text(s)                                      # a cell with plain text
    out = []
    for x in items[:MAX_SCENES]:
        c = _clean(x)
        if c["description"] or c["title"] or c["source"] == "file":
            out.append(c)
    for i, c in enumerate(out, 1):
        c["order"] = i
    return out


def has_file(c: dict) -> bool:
    """A file scene whose file really is on this machine."""
    return c.get("source") == "file" and bool(c.get("local_path")) and Path(c["local_path"]).exists()


def scenes_sig(scenes) -> str:
    """Stable fingerprint of what the USER entered (not of paths on this machine): used for the work-folder name."""
    key = [[c.get("title", ""), c.get("description", ""), c.get("source", ""), c.get("engine", ""),
            c.get("file_id", "") or c.get("url", ""), c.get("file_name", "")] for c in scenes or []]
    return hashlib.md5(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
