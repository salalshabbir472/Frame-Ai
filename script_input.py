"""
script_input.py - stage 1 for videos whose script is supplied by the user (Input Type = Script).

The normal stage 1 (stages_gemini.stage_script) WRITES a script from a topic. Here the script already
exists, so the narration is used word for word. This module only:
  1. cleans the text   (markdown, SRT cues, stray symbols; headings are kept as scene titles)
  2. splits it into scenes of about `scene_sec` seconds each, on sentence boundaries
  3. gives every scene a short heading (markdown heading if there is one, else ONE cheap Gemini call,
     else the first words of the scene)
and returns exactly what stage_script returns:  {"title", "language", "scenes": [{id, heading, narration}]}
so stages 2-6 (audio, analysis, Manim, assemble) run unchanged.
"""
from __future__ import annotations

import json
import logging
import re

from config import LANGS, as_scenes, split_sentences
from gemini_client import QuotaExhausted

log = logging.getLogger("edu")

END_PUNCT = re.compile(r"[.!?؟۔…:;]\s*$")
MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
SRT_INDEX = re.compile(r"(?m)^\s*\d+\s*$")
SRT_TIME = re.compile(r"(?m)^\s*\d{1,2}:\d\d:\d\d[,.]\d{1,3}\s*-->.*$")


def clean_script(text: str) -> str:
    """Plain narration text. Keeps blank lines (paragraphs) and markdown headings (lines starting with #)."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    if SRT_TIME.search(t):                       # an uploaded .srt: drop cue numbers and time codes
        t = SRT_TIME.sub("", t)
        t = SRT_INDEX.sub("", t)
    t = re.sub(r"<[^>]+>", "", t)                 # <i>, <br>, other tags
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)   # markdown links / images -> their text
    t = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", t)      # bold / strike
    t = re.sub(r"(?<![\w*])[*_](\S.*?\S|\S)[*_](?![\w*])", r"\1", t)   # italics
    t = re.sub(r"`+", "", t)
    t = re.sub(r"(?m)^\s*(?:[-*+•]|\d{1,2}[.)])\s+", "", t)   # bullet / numbered list markers
    t = re.sub(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", t)  # horizontal rules
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _sections(text: str):
    """[(heading or None, body)] - a markdown heading starts a new section and is NOT spoken."""
    out, head, buf = [], None, []
    for line in text.split("\n"):
        m = MD_HEADING.match(line)
        if m:
            if "".join(buf).strip():
                out.append((head, "\n".join(buf)))
            head, buf = (m.group(1).strip() or None), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        out.append((head, "\n".join(buf)))
    return out


def _sentences(body: str, lang: str):
    """Sentences of one section. Hard-wrapped lines (PDF / Word) are joined; if most lines carry their own
    full stop, line breaks are kept as sentence breaks; a line with no end mark gets one."""
    end = "۔" if lang == "ur" else "."
    sents = []
    for para in re.split(r"\n\s*\n", body):
        lines = [l.strip() for l in para.split("\n") if l.strip()]
        if not lines:
            continue
        ended = sum(bool(END_PUNCT.search(l)) for l in lines)
        if len(lines) > 1 and ended / len(lines) < 0.3:
            text = " ".join(lines)                       # wrapped prose: one flowing paragraph
        else:
            text = " ".join(l if END_PUNCT.search(l) else l + end for l in lines)
        text = text.strip()
        if text and not END_PUNCT.search(text):
            text += end
        sents += split_sentences(text)
    return [s for s in sents if s.strip()]


def _words(s: str) -> int:
    return max(1, len(s.split()))


def _group(sents, target):
    """Consecutive sentences -> scenes of about `target` words, evenly sized."""
    total = sum(_words(s) for s in sents)
    n = max(1, round(total / target))
    per = total / n
    groups, done = [[] for _ in range(n)], 0
    for s in sents:
        w = _words(s)
        groups[min(n - 1, int((done + w / 2) / per))].append(s)
        done += w
    return [" ".join(g) for g in groups if g]


def split_script(text: str, lang: str, scene_sec: float, max_scenes: int):
    """[(heading or None, narration)] in order. Never more than `max_scenes` scenes."""
    secs = []
    for head, body in _sections(clean_script(text)):
        sents = _sentences(body, lang)
        if sents:
            secs.append((head, sents))
    if not secs:
        return []
    target = max(8.0, LANGS[lang]["wps"] * scene_sec)
    while True:
        scenes = [(h if i == 0 else None, n) for h, ss in secs for i, n in enumerate(_group(ss, target))]
        if len(scenes) <= max_scenes or len(scenes) <= len(secs):
            return scenes
        target *= 1.25


def _fallback_heading(narration: str, n=6) -> str:
    w = narration.split()
    return " ".join(w[:n]).rstrip(".!?؟۔…,;:") + ("..." if len(w) > n else "")


def _headings(gem, cfg, scenes):
    """One light Gemini call per ~12 scenes, for scenes that have no heading. Cosmetic: any failure except a
    quota wall falls back to the first words of the scene."""
    need = [i for i, (h, _) in enumerate(scenes) if not h]
    out = {}
    for k in range(0, len(need), 12):
        part = need[k:k + 12]
        items = [{"id": i + 1, "narration": scenes[i][1][:400]} for i in part]
        try:
            raw = gem.json(f"""Give each scene of this narrated video a SHORT on-screen heading (2-5 words).
Language of the headings: {LANGS[cfg.screen_language]['name']}. Do not rewrite or judge the narration.
Video title: {cfg.topic}
Scenes: {json.dumps(items, ensure_ascii=False)}
Return JSON only: {{"scenes": [{{"id": 1, "heading": "..."}}]}}""", temperature=0.3, task="analyze")
            for x in as_scenes(raw):
                try:
                    out[int(x.get("id"))] = str(x.get("heading", "")).strip()
                except (TypeError, ValueError):
                    pass
        except QuotaExhausted:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("  script: headings skipped for scenes %s (%s)", [i + 1 for i in part], str(e)[:100])
    return out


def stage_script_text(gem, cfg, work=None):
    """Stage 1 for Input Type = Script. Same return shape as stages_gemini.stage_script."""
    parts = split_script(cfg.script, cfg.language, cfg.scene_sec, cfg.max_scenes)
    if not parts:
        raise ValueError("the script has no readable text")
    words = sum(_words(n) for _, n in parts)
    log.info("  script: using your script as written: %d words -> %d scene(s), about %.1f min",
             words, len(parts), words / LANGS[cfg.language]["wps"] / 60)
    heads = _headings(gem, cfg, parts)
    scenes = [{"id": i + 1, "heading": h or heads.get(i + 1) or _fallback_heading(n) or f"Part {i + 1}",
               "narration": n} for i, (h, n) in enumerate(parts)]
    return {"title": cfg.topic, "language": cfg.language, "scenes": scenes}
