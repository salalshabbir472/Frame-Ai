"""
script_input.py - stage 1 for videos whose script is supplied by the user (Input Type = Script).

The normal stage 1 (stages_gemini.stage_script) WRITES a script from a topic. Here the script already
exists, so the narration is used word for word. This module only:
  1. cleans the text   (markdown, SRT cues, stray symbols; headings are kept as scene titles), then Gemini removes
                       notes / metadata / stage directions and fixes punctuation. It may not change a word: the code
                       compares every paragraph with the original and throws away any paragraph whose wording changed.
  2. splits it into scenes of about `scene_sec` seconds each, on sentence boundaries
  3. gives every scene a short heading (markdown heading if there is one, else ONE cheap Gemini call,
     else the first words of the scene)
and returns exactly what stage_script returns:  {"title", "language", "scenes": [{id, heading, narration}]}
so stages 2-6 (audio, analysis, Manim, assemble) run unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

from config import LANGS, as_scenes, load_json, save_json, split_sentences
from gemini_client import QuotaExhausted
from stages_gemini import screen_rules

log = logging.getLogger("edu")

END_PUNCT = re.compile(r"[.!?؟۔…:;]\s*$")
MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
SRT_INDEX = re.compile(r"(?m)^\s*\d+\s*$")
HTML_TAG = re.compile(r"</?(?:b|i|u|s|em|strong|br|p|span|div|font|sub|sup|a|center|h[1-6]|li|ul|ol)"
                      r"(?:\s+[\w:-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))*\s*/?>", re.I)
SRT_TIME = re.compile(r"(?m)^\s*\d{1,2}:\d\d:\d\d[,.]\d{1,3}\s*-->.*$")


def clean_script(text: str) -> str:
    """Plain narration text. Keeps blank lines (paragraphs) and markdown headings (lines starting with #)."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    if SRT_TIME.search(t):                       # an uploaded .srt: drop cue numbers and time codes
        t = SRT_TIME.sub("", t)
        t = SRT_INDEX.sub("", t)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)   # a line break tag is a break, not nothing
    t = HTML_TAG.sub("", t)                       # <i>, <br>, other real tags (maths like "a < b and c > d" stays)
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
Language of the headings: {LANGS[cfg.screen_language]['name']}. {screen_rules(cfg)} Do not rewrite or judge the narration.
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



# --------------------------------------------------------------------------- #
# Gemini clean-up: remove what is not narration, fix the FORM, never touch the words
# --------------------------------------------------------------------------- #
CLEAN_CHARS = 4500          # characters of script per Gemini call
LONG_NOTE = 80              # a paragraph this long that Gemini calls "not narration" is kept (and logged) instead
KEEP_AT_LEAST = 0.5         # if Gemini would delete more than half of the words, something is wrong: ignore it
_KEEP_CHARS = re.compile(r"[^\w\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]+")   # Latin, digits, all Urdu/Arabic
_UR_PUNCT = re.compile("[\u060c\u061b\u061f\u066a-\u066d\u06d4]")   # ، ؛ ؟ ٪ ٫ ٬ ٭ ۔  live in the Arabic block but are punctuation


def _norm_words(text: str):
    """[(word as written, word without punctuation / case)] - words that are only punctuation are skipped."""
    out = []
    for w in text.split():
        n = _KEEP_CHARS.sub("", _UR_PUNCT.sub("", w)).lower()
        if n:
            out.append((w, n))
    return out


def _compare(original: str, cleaned: str):
    """(words Gemini ADDED or changed, text it REMOVED). Punctuation, spacing and line breaks are ignored, so only
    real wording changes count."""
    a, b = _norm_words(original), _norm_words(cleaned)
    sm = SequenceMatcher(None, [x[1] for x in a], [x[1] for x in b], autojunk=False)
    added, removed = 0, []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            added += j2 - j1
        if tag in ("replace", "delete"):
            removed.append(" ".join(w for w, _ in a[i1:i2]))
    return added, removed


def _only_deletions(original: str, cleaned: str):
    """Gemini touched the wording of this paragraph. Keep the user's own words and apply ONLY what Gemini removed
    (notes, labels, metadata). Returns (text, removed pieces)."""
    a, b = _norm_words(original), _norm_words(cleaned)
    sm = SequenceMatcher(None, [x[1] for x in a], [x[1] for x in b], autojunk=False)
    keep, removed = [], []
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        words = [w for w, _ in a[i1:i2]]
        if tag == "delete":
            removed.append(" ".join(words))
            end = re.search(r"[.!?\u061f\u06d4\u2026]+$", words[-1]) if words else None
            if end and keep and not re.search(r"[.!?\u061f\u06d4\u2026:;]$", keep[-1]):
                keep[-1] += end.group(0)        # "...everyone [music]." must still end the sentence
        else:                       # equal, or replace / insert: the original words stay
            keep += words
    return " ".join(keep), removed


def _tidy(t: str) -> str:
    """No space before sentence punctuation (what is left after a note was cut out of a sentence)."""
    return re.sub(r"[ \t]+([.!?,;:\u060c\u061b\u061f\u06d4\u2026])", r"\1", t)


def _blocks(text: str):
    """[(is_heading, text)] - paragraphs; a markdown heading line is its own block and is never sent to Gemini."""
    out, buf = [], []

    def flush():
        if buf:
            out.append((False, "\n".join(buf).strip()))
            buf.clear()

    for line in text.split("\n"):
        if MD_HEADING.match(line):
            flush()
            out.append((True, line.strip()))
        elif not line.strip():
            flush()
        else:
            buf.append(line)
    flush()
    return [b for b in out if b[1]]


def _clean_prompt(cfg, items):
    end = "۔ (or ؟)" if cfg.language == "ur" else ". (or ! ?)"
    mixed = ("\nThe script is Urdu with English words and technical terms mixed in (for example \"gradient descent\", "
             "\"model\", \"accuracy\"). That is intentional: keep every English word exactly as written, in English "
             "letters. Do not translate, transliterate or replace any word." if cfg.language == "ur" else "")
    return f"""You are a script editor preparing a FINISHED voice-over script for text-to-speech.
The script belongs to the user. THE WORDS ARE FINAL: never reword, translate, shorten, summarise, reorder, add,
"improve" or fix the grammar or spelling of any spoken word.{mixed}

Do exactly two things to every block:
1. REMOVE what is not meant to be spoken: titles, author / date / version / copyright lines, labels such as
   "Script:", "Narration:", "Voice over:", speaker names ("Narrator:", "Speaker 1:"), scene / shot / slide
   numbers and labels, timestamps, stage and sound directions in brackets or parentheses ([music], (pause), (SFX)),
   "Visual:" / "On screen:" / "B-roll:" notes, editor comments, TODO notes, URLs, file names, hashtags,
   emoji, word counts, page numbers, headers and footers, and any other metadata or stray detail.
   If a block is ONLY such material, return "keep": false.
2. FIX THE FORM only: spacing, line breaks and punctuation, so the text reads as clean sentences and every
   sentence ends with {end}. Join a sentence that was broken across lines. Do not change a single word.
A block that is already clean must come back exactly as it is. When unsure whether something is narration, KEEP it.

Blocks: {json.dumps(items, ensure_ascii=False)}

Return JSON only: {{"blocks": [{{"id": 1, "keep": true, "text": "..."}}]}}"""


def ai_clean(gem, cfg, text: str, work=None):
    """Gemini removes notes / metadata and fixes the form of a user script. The code then CHECKS word by word that
    Gemini changed nothing else: in a paragraph whose wording was touched only Gemini's deletions are applied (the
    narration stays the user's own words), and if Gemini would remove more than half of the script its answer is
    ignored.
    Returns (text, report) with report = what was removed."""
    blocks = _blocks(text)
    path = (Path(work) / "1_script_clean.json") if work else None
    done = load_json(path, {}) if path else {}
    if not isinstance(done, dict):
        done = {}
    key = lambda t: hashlib.md5(f"{cfg.language}|{t}".encode()).hexdigest()   # noqa: E731
    todo = [i for i, (head, t) in enumerate(blocks) if not head and key(t) not in done]
    batches, cur, size = [], [], 0
    for i in todo:
        if cur and size + len(blocks[i][1]) > CLEAN_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(blocks[i][1])
    if cur:
        batches.append(cur)
    for n, part in enumerate(batches, 1):
        log.info("  script: cleaning notes / punctuation with Gemini (%d/%d)", n, len(batches))
        try:
            raw = gem.json(_clean_prompt(cfg, [{"id": k + 1, "text": blocks[i][1]} for k, i in enumerate(part)]),
                           temperature=0.1, task="outline")
        except QuotaExhausted:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("  script: Gemini clean-up skipped for this part (%s); using the basic clean-up", str(e)[:100])
            continue
        got = {}
        for x in (raw.get("blocks") if isinstance(raw, dict) else raw) or []:
            try:
                got[int(x.get("id"))] = x
            except (AttributeError, TypeError, ValueError):
                pass
        for k, i in enumerate(part):
            orig, x = blocks[i][1], got.get(k + 1)
            if not x:
                continue                                   # no answer for this block: it stays as written
            new = str(x.get("text") or "").strip()
            keep = x.get("keep", True) not in (False, "false", "False", 0)
            if (not keep or not new) and len(_norm_words(orig)) >= LONG_NOTE:
                log.warning("  script: Gemini called a long paragraph (%d words) a note, but it looks like narration: "
                            "kept it: %s", len(_norm_words(orig)), orig[:60].replace("\n", " "))
                done[key(orig)] = {"keep": True, "text": orig, "removed": []}
                continue
            if not keep or not new:
                done[key(orig)] = {"keep": False, "text": "", "removed": [orig]}
                continue
            added, removed = _compare(orig, new)
            if added:
                kept, removed = _only_deletions(orig, new)
                log.warning("  script: Gemini changed %d word(s) of one paragraph; your words were kept and only the "
                            "removed notes were applied: %s", added, orig[:60].replace("\n", " "))
                done[key(orig)] = {"keep": bool(kept.strip()), "text": _tidy(kept), "removed": removed}
                continue
            done[key(orig)] = {"keep": True, "text": _tidy(new), "removed": removed}
        if path:
            save_json(path, done)
    out, removed, kept_w, all_w = [], [], 0, 0
    for head, t in blocks:
        w = len(_norm_words(t))
        all_w += w
        r = done.get(key(t)) if not head else None
        if head or r is None:
            out.append(t)
            kept_w += w
            continue
        removed += r["removed"]
        if r["keep"]:
            out.append(r["text"])
            kept_w += len(_norm_words(r["text"]))
    if all_w and kept_w < KEEP_AT_LEAST * all_w:
        log.warning("  script: Gemini wanted to remove %d%% of the script; ignoring it and using the basic clean-up",
                    round(100 * (1 - kept_w / all_w)))
        return text, []
    if path:
        save_json(Path(work) / "1_script_removed.json", removed)
    if removed:
        log.info("  script: removed %d note / metadata piece(s), e.g. %s", len(removed),
                 " | ".join(r[:50] for r in removed[:3]))
    return "\n\n".join(out), removed


def stage_script_text(gem, cfg, work=None):
    """Stage 1 for Input Type = Script. Same return shape as stages_gemini.stage_script."""
    text = clean_script(cfg.script)
    if cfg.script_cleanup:
        try:
            text, _ = ai_clean(gem, cfg, text, work)       # notes / metadata out, form fixed, wording untouched
        except QuotaExhausted:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("  script: Gemini clean-up failed (%s); using the basic clean-up", str(e)[:100])
    parts = split_script(text, cfg.language, cfg.scene_sec, cfg.max_scenes)
    if not parts:
        raise ValueError("the script has no readable text")
    words = sum(_words(n) for _, n in parts)
    log.info("  script: using your script as written: %d words -> %d scene(s), about %.1f min",
             words, len(parts), words / LANGS[cfg.language]["wps"] / 60)
    heads = _headings(gem, cfg, parts)
    scenes = [{"id": i + 1, "heading": h or heads.get(i + 1) or _fallback_heading(n) or f"Part {i + 1}",
               "narration": n} for i, (h, n) in enumerate(parts)]
    return {"title": cfg.topic, "language": cfg.language, "scenes": scenes}
