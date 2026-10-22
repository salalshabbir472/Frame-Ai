"""
research.py - stage 0: ONE Google Search research pass per video (Prompt, Script and Source videos alike).

The notes + sources are saved in build/<slug>/0_research.json and handed to the later stages as background facts.
Search is never skipped: if cfg.web_research is on and the search fails (quota aside, which retries next run),
the video fails with a clear message instead of being written without it.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from config import load_json, save_json
from gemini_client import QuotaExhausted

log = logging.getLogger("edu")

NOTES_MAX = 6000        # characters of notes handed to every later prompt


def _subject(cfg) -> str:
    """What to research: the title/topic, plus a short piece of the user's own text for Script / Source videos."""
    text = cfg.script if cfg.input_type == "script" else cfg.source_text if cfg.input_type == "source" else ""
    bits = [f"Title / topic: {cfg.topic}"]
    if text:
        bits.append("The video will be about this text (start of it):\n" + text[:1200])
    if cfg.custom_instruction:
        bits.append(f"User instruction: {cfg.custom_instruction}")
    return "\n".join(bits)


def stage_research(gem, cfg, work: Path) -> dict:
    subject = _subject(cfg)
    sig = hashlib.md5(f"{subject}|{cfg.audience}".encode()).hexdigest()
    path = Path(work) / "0_research.json"
    saved = load_json(path, None)
    if isinstance(saved, dict) and saved.get("sig") == sig and saved.get("notes"):
        log.info("  (cached) 0_research.json")
        return saved
    log.info("[0/6] Web research (Google Search)")
    prompt = f"""Search the web and collect accurate, current background facts for a narrated educational video.
{subject}
Audience: {cfg.audience}.
Write plain-text notes IN ENGLISH (whatever the video language is): key facts, definitions, dates, numbers, how things
work, common misconceptions, and good real examples. Prefer reliable sources. Where sources disagree, say so.
Do not write the video script. Maximum about 900 words."""
    try:
        notes, sources = gem.search(prompt)
    except QuotaExhausted:
        raise
    except Exception as e:  # noqa: BLE001 - search is required: fail loudly, never write without it
        raise RuntimeError(f"Web research failed ({str(e)[:200]}). Turn Web research off for this video "
                           f"(or the project) if you want it made without searching.") from e
    seen, uniq = set(), []
    for s in sources:
        if s["url"] and s["url"] not in seen:
            seen.add(s["url"])
            uniq.append(s)
    data = {"sig": sig, "notes": notes.strip(), "sources": uniq[:12]}
    save_json(path, data)
    log.info("  research: %d chars of notes, %d source(s)", len(data["notes"]), len(uniq))
    return data


def research_block(cfg) -> str:
    """Text to put into a prompt ('' when there are no notes)."""
    notes = (getattr(cfg, "research_notes", "") or "").strip()
    if not notes:
        return ""
    return ("BACKGROUND FACTS FROM A GOOGLE SEARCH (treat as the reliable reference; do not contradict them, do not "
            "invent facts beyond them and your own solid knowledge, do not mention that a search happened):\n"
            + notes[:NOTES_MAX] + "\n")
