"""
stages_gemini.py - every pipeline stage that CALLS Gemini (text, TTS or code).

Contains: language prompt blocks, then
  stage 1  stage_script   -> script          (gem.json, plan -> chapters -> scenes)
  stage 2  stage_audio    -> narration WAVs  (tts.speak_many) + sentence timelines
  stage 3  stage_analyze  -> concepts/key points/visuals (gem.json)
  stage 4  stage_spec     -> Manim spec pinned to sentences (gem.json) + pin_steps
  stage 5  stage_manim    -> Gemini-written Manim code + render with auto-repair (gem.code)
  stage 6  stage_sfx      -> per-scene narration + sound-effects mix (sfx.py, no Gemini call)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from config import LANGS, as_scenes, duration, load_json, save_json, split_sentences
from gemini_client import BadOutput
from sfx import build_scene_mix
from media import (build_timeline, fallback_code, precheck, render_manim,
                   write_scene_file)

log = logging.getLogger("edu")


# --------------------------------------------------------------------------- #
# Language prompt blocks
# --------------------------------------------------------------------------- #
def narration_rules(cfg):
    if cfg.language == "ur":
        return ("Write natural, SPOKEN Urdu in Urdu (Arabic) script - never Roman Urdu. The audience speaks everyday "
                "Urdu and understands English well, so write the way a friendly teacher TALKS: simple, common, "
                "everyday words. Avoid heavy, bookish, formal or Persian-style Urdu words and long compound phrases; "
                "if a plain word exists, use it. Keep English technical terms and common English words in ENGLISH "
                "letters, as people really say them (e.g. gradient descent, model, data, accuracy, error): never "
                "invent a difficult Urdu translation for a term and never put a translation in brackets. "
                "Use the same English spelling for a term every time. Use Urdu punctuation (۔ and ؟) and end every "
                "Urdu sentence with it. Prefer short sentences. Small numbers as Urdu words.")
    return "Write clear, natural spoken English. End every sentence with . ! or ?"


def screen_rules(cfg):
    if cfg.screen_language == "ur":
        return ("All text shown on screen must be Urdu in Urdu script, in short, simple everyday words. A well-known "
                "English technical term (the one the narrator says in English) may be written in English letters, "
                "but as its OWN short label; never mix English and Urdu letters inside one label.")
    return "All text shown on screen must be short, clear English."


# --------------------------------------------------------------------------- #
# Stage 1: script  (hierarchical: plan -> chapters -> scenes -> narration)
#
# One giant prompt loses the thread after ~2 minutes of content (repetition, drifting terms,
# scenes too short/long). So a long video is written like a book:
#   1. PLAN   one call: title, viewer goal, ONE running example, fixed terms, k chapters
#   2. OUTLINE one call per chapter (~2 min each): exact number of scenes + the idea of each
#   3. WRITE  narration in small batches; every batch sees the brief, what is already covered
#             and the last lines spoken, so the video flows as one story
#   4. CHECK  word count, sentence ending and repetition are verified in code; bad scenes are
#             rewritten once with the exact problem named
# Everything is saved to 1_script_parts.json after each step, so a quota wall just resumes.
# --------------------------------------------------------------------------- #
END_PUNCT = re.compile(r"[.!?؟۔…]\s*$")


def _fit_counts(raw, k, n):
    """k chapter scene-counts that sum to exactly n, proportional to Gemini's weights."""
    try:
        c = [max(1, int(x)) for x in raw][:k]
    except (TypeError, ValueError):
        c = []
    if len(c) != k:
        c = [1] * k
    tot = sum(c)
    c = [max(1, round(x * n / tot)) for x in c]
    while sum(c) > n and max(c) > 1:
        c[c.index(max(c))] -= 1
    while sum(c) < n:
        c[c.index(min(c))] += 1
    return c


def _shingles(text, size=5):
    w = text.lower().split()
    return {tuple(w[i:i + size]) for i in range(max(0, len(w) - size + 1))}


def _problems(text, words, seen):
    out = []
    n = len(text.split())
    if n < 0.6 * words:
        out.append(f"too short ({n} words; write about {words})")
    elif n > 1.5 * words:
        out.append(f"too long ({n} words; write about {words})")
    if not END_PUNCT.search(text):
        out.append("must end with . ! ? or ۔")
    sh = _shingles(text)
    if sh and len(sh & seen) / len(sh) > 0.3:
        out.append("repeats wording from earlier scenes - say it differently or move on")
    return out


def _brief_text(plan):
    terms = plan.get("terms") or []
    if isinstance(terms, dict):
        terms = [{"term": k, "meaning": v} for k, v in terms.items()]
    t = "; ".join(f"{x.get('term')} = {x.get('meaning')}" for x in terms if isinstance(x, dict))
    return (f"Title: {plan['title']}\nViewer goal: {plan.get('goal', '')}\n"
            f"Running example (reuse it, keep it consistent): {plan.get('running_example', '')}\n"
            f"Terms (use exactly these names every time): {t}")


def _make_plan(gem, cfg, k, n):
    raw = gem.json(f"""You are an expert teacher planning ONE coherent, HEAVILY VISUAL narrated video.
Topic: {cfg.topic}
Audience: {cfg.audience}
Length: about {cfg.minutes} minutes, written in {LANGS[cfg.language]['name']}.
Plan it as exactly {k} chapters (about {cfg.chapter_min:g} minutes each).
RULES: chapters build strictly on each other; every concept is introduced in exactly ONE chapter
and only reused afterwards; chapter 1 = hook + why it matters; last chapter = worked example + recap.
Pick ONE concrete running example or analogy that every chapter returns to, so it feels like one story.
"terms" = the 4-10 key terms, written the way the narrator will say them in {LANGS[cfg.language]['name']}.
Return JSON only:
{{"title": "...", "goal": "what the viewer understands/can do at the end",
 "running_example": "...", "terms": [{{"term": "...", "meaning": "one plain sentence"}}],
 "chapters": [{{"title": "...", "goal": "...", "concepts": ["..."], "weight": 3}}]}}
("weight" 1-5 = how much of the video this chapter deserves.)""", temperature=0.7, task="plan")
    if not isinstance(raw, dict):
        raw = {}
    chs = [c for c in (raw.get("chapters") or []) if isinstance(c, dict)][:k]
    if not chs:
        raise BadOutput("script plan had no chapters")
    counts = _fit_counts([c.get("weight", 1) for c in chs], len(chs), n)
    return {"title": str(raw.get("title") or cfg.topic), "goal": str(raw.get("goal", "")),
            "running_example": str(raw.get("running_example", "")),
            "terms": raw.get("terms") or [],
            "chapters": [{"title": str(c.get("title") or f"Chapter {i + 1}"),
                          "goal": str(c.get("goal", "")), "concepts": c.get("concepts") or [],
                          "scenes": counts[i]} for i, c in enumerate(chs)]}


def _make_outline(gem, cfg, plan, ci, covered, first_id):
    ch = plan["chapters"][ci]
    want = ch["scenes"]
    chapters = [f"{i + 1}. {c['title']} - {c['goal']}" for i, c in enumerate(plan["chapters"])]
    items = []
    for _ in range(2):
        raw = gem.json(f"""You are an expert teacher outlining one chapter of a narrated, HEAVILY VISUAL video.
{_brief_text(plan)}
All chapters: {json.dumps(chapters, ensure_ascii=False)}
Already covered in earlier scenes (do NOT teach again): {json.dumps(covered[-15:], ensure_ascii=False)}
Now outline chapter {ci + 1}/{len(plan['chapters'])}: {json.dumps(ch, ensure_ascii=False)}
Return EXACTLY {want} scenes. Each teaches ONE idea that can be DRAWN (picture, analogy, moving
diagram or worked number example) and builds on the previous scene.
{'The first scene of the whole video is the hook.' if ci == 0 else ''}{' The last scene is the recap: no new ideas.' if ci == len(plan['chapters']) - 1 else ''}
Return JSON only: {{"scenes": [{{"heading": "...", "idea": "the one thing shown, and the picture used"}}]}}""",
                       task="outline")
        items = [x for x in as_scenes(raw) if x.get("heading") or x.get("idea")]
        if len(items) >= want:
            break
    return [{"id": first_id + i, "heading": str(x.get("heading") or f"Part {first_id + i}"),
             "idea": str(x.get("idea", ""))} for i, x in enumerate(items[:want])]


def _write_batch(gem, cfg, plan, ci, batch, words, covered, prev_tail, is_first, is_last, notes=""):
    ch = plan["chapters"][ci]
    nxt = (plan["chapters"][ci + 1]["title"] if ci + 1 < len(plan["chapters"]) else "")
    lo, hi = int(words * 0.7), int(words * 1.3)
    extra = []
    if is_first:
        extra.append("The very first scene opens the video with a hook (a question or surprising picture).")
    if is_last:
        extra.append("The very last scene is a short recap and a warm closing line. No new ideas.")
    if not is_first:
        extra.append("Do NOT greet or say 'welcome' / 'in this video' again.")
    if not is_last:
        extra.append("Do NOT say 'in conclusion' or 'to summarize'; do not wrap up the video.")
    raw = gem.json(f"""You are an expert teacher writing a narrated educational video.
Topic: {cfg.topic}. Audience: {cfg.audience}.
Language: {LANGS[cfg.language]['name']}. {narration_rules(cfg)}
VIDEO BRIEF
{_brief_text(plan)}
CHAPTER {ci + 1}/{len(plan['chapters'])}: {json.dumps(ch, ensure_ascii=False)}
{('Next chapter (do not start it): ' + nxt) if nxt else ''}
ALREADY COVERED (build on it, never re-explain): {json.dumps(covered[-15:], ensure_ascii=False)}
The narration just before these scenes ended with: "{prev_tail}"
Continue naturally from that. Do not repeat it.
Write the narration ONLY for these scenes: {json.dumps(batch, ensure_ascii=False)}
Each narration is about {words} words (allowed {lo}-{hi}). RULES: SHORT sentences (8-14 words).
Every sentence must describe something that can be SHOWN on screen: a picture, an analogy, a
moving diagram, or a worked number example. Reuse the running example and the exact terms above.
No markdown, stage directions or emojis. {' '.join(extra)}
{notes}
Return JSON only: {{"scenes": [{{"id": 1, "heading": "...", "narration": "..."}}]}}""", temperature=0.6, task="write")
    got = {}
    items = as_scenes(raw)
    for j, x in enumerate(items):
        nar = str(x.get("narration", "")).strip()
        try:
            sid = int(x.get("id"))
        except (TypeError, ValueError):
            sid = batch[j]["id"] if j < len(batch) else None
        if nar and sid is not None:
            got[sid] = nar
    return got


def stage_script(gem, cfg, work=None):
    wps = LANGS[cfg.language]["wps"]
    n = max(4, min(cfg.max_scenes, round(cfg.minutes * 60 / cfg.scene_sec)))
    words = int(cfg.minutes * 60 * wps / n)
    k = 1 if cfg.minutes <= 4 else max(2, min(n, round(cfg.minutes / cfg.chapter_min)))
    sig = hashlib.md5(f"{cfg.topic}|{cfg.minutes}|{cfg.audience}|{cfg.language}|{cfg.scene_sec}|"
                      f"{cfg.max_scenes}|{cfg.chapter_min}".encode()).hexdigest()
    path = (Path(work) / "1_script_parts.json") if work else None
    state = load_json(path, {}) if path else {}
    if not isinstance(state, dict) or state.get("sig") != sig:
        state = {}
    state["sig"] = sig

    def save():
        if path:
            save_json(path, state)

    if "plan" not in state:
        log.info("  script: planning %d chapter(s) for ~%d scenes", k, n)
        state["plan"] = _make_plan(gem, cfg, k, n)
        save()
    plan = state["plan"]
    chs = state.setdefault("chapters", [])

    for ci, ch in enumerate(plan["chapters"]):
        while len(chs) <= ci:
            chs.append({})
        st = chs[ci]
        if "outline" not in st:
            covered = [f"{s['heading']}: {s['idea']}" for c in chs[:ci] for s in c.get("outline", [])]
            first_id = 1 + sum(len(c.get("outline", [])) for c in chs[:ci])
            st["outline"] = _make_outline(gem, cfg, plan, ci, covered, first_id)
            save()
        outline = st["outline"]
        done = st.setdefault("scenes", [])
        while len(done) < len(outline):
            batch = outline[len(done): len(done) + cfg.scene_batch]
            before = [s for c in chs[:ci + 1] for s in c.get("scenes", [])]
            covered = [f"{s['heading']}: {s['idea']}" for c in chs[:ci] for s in c.get("outline", [])]
            covered += [f"{s['heading']}: {s['idea']}" for s in outline[:len(done)]]
            tail = " ".join(split_sentences(before[-1]["narration"])[-2:]) if before else ""
            seen = set().union(*[_shingles(s["narration"]) for s in before]) if before else set()
            is_first = batch[0]["id"] == 1
            is_last = ci == len(plan["chapters"]) - 1 and batch[-1]["id"] == outline[-1]["id"]
            args = (gem, cfg, plan, ci)
            got = _write_batch(*args, batch, words, covered, tail, is_first, is_last)
            bad = {}
            for s in batch:
                pr = _problems(got.get(s["id"], ""), words, seen) if got.get(s["id"]) else ["missing"]
                if pr:
                    bad[s["id"]] = pr
            if bad:
                log.info("  script: rewriting scene(s) %s: %s", list(bad), "; ".join(
                    f"{i}: {', '.join(p)}" for i, p in bad.items()))
                redo = [s for s in batch if s["id"] in bad]
                notes = "FIX THESE PROBLEMS from your previous attempt: " + "; ".join(
                    f"scene {i}: {', '.join(p)}" for i, p in bad.items())
                got2 = _write_batch(*args, redo, words, covered, tail,
                                    redo[0]["id"] == 1, is_last and redo[-1]["id"] == batch[-1]["id"], notes)
                for i in bad:
                    better = bool(got2.get(i)) and (
                        not got.get(i) or len(_problems(got2[i], words, seen)) <= len(bad[i]))
                    if better:
                        got[i] = got2[i]
            for s in batch:
                nar = got.get(s["id"]) or (s["idea"].rstrip(".") + ".")
                if not got.get(s["id"]):
                    log.warning("  script: scene %s has no narration; using its idea as a stand-in", s["id"])
                done.append({**s, "narration": nar})
            save()
        log.info("  script: chapter %d/%d done (%d scenes)", ci + 1, len(plan["chapters"]), len(done))

    scenes = [{"id": i + 1, "heading": s["heading"], "narration": s["narration"]}
              for i, s in enumerate(x for c in chs for x in c.get("scenes", []))]
    if not scenes:
        raise BadOutput("script had no scenes")
    return {"title": plan["title"], "language": cfg.language, "scenes": scenes}


# --------------------------------------------------------------------------- #
# Stage 2: narration audio  ->  real durations + per-sentence timeline
# --------------------------------------------------------------------------- #
def stage_audio(tts, cfg, script, work):
    audio_dir = work / "audio"
    audio_dir.mkdir(exist_ok=True)
    info_path = work / "2_audio.json"
    info = load_json(info_path, {})
    pending, meta = [], {}
    for s in script["scenes"]:
        sid = str(s["id"])
        wav = audio_dir / f"scene_{s['id']:02d}.wav"
        sig = hashlib.md5(f"{s['narration']}|{cfg.voice}|{cfg.tts_style}|{cfg.tts_engine}|"
                          f"{cfg.tts_models}".encode()).hexdigest()
        have = info.get(sid)
        if have and have["sig"] == sig and wav.exists() and wav.stat().st_size > 0:
            log.info("  scene %s audio cached (%.1fs)", sid, have["duration"])
            continue
        pending.append((s["id"], s["narration"], wav))
        meta[s["id"]] = (sig, s["narration"])

    def done(sid, wav):                      # called per scene as soon as its wav exists
        sig, text = meta[sid]
        d = duration(wav)
        info[str(sid)] = {"file": str(wav.resolve()), "duration": round(d, 3), "sig": sig,
                          "timeline": build_timeline(text, wav, d)}
        save_json(info_path, info)   # per-scene resume
        log.info("  scene %s: %.1fs narration, %d sentences", sid, d, len(info[str(sid)]["timeline"]))

    if pending:
        log.info("  synthesizing %d scene(s), %d chars total", len(pending),
                 sum(len(p[1]) for p in pending))
        tts.speak_many(pending, cfg, on_done=done)
    return {int(k): v for k, v in info.items()}


# --------------------------------------------------------------------------- #
# Stage 3: concepts / examples / key points / visuals
# --------------------------------------------------------------------------- #
def stage_analyze(gem, cfg, script, batch=5):
    """Batched: a 10-minute script (40 scenes x 4-7 visuals) does not fit one reliable answer."""
    out = []
    for i in range(0, len(script["scenes"]), batch):
        part = script["scenes"][i:i + batch]
        raw = gem.json(f"""Analyze this part of an educational video script scene by scene.
Video title: {script['title']}
For each scene extract:
- concepts: key ideas introduced (short phrases)
- examples: concrete examples used or that would help (short phrases)
- key_points: 1-3 takeaways a student should remember (short sentences)
- visuals: 4-7 DIFFERENT visuals that explain the narration. Each has "type" (one of: text, equation,
  graph, number_line, diagram, flowchart, shapes, table, comparison, process, timeline,
  analogy, data_flow, zoom_in, before_after, counter, worked_example, network, grid, stack)
  and "description" (English, for the animator). Prefer pictures over words: explain every idea
  with an analogy built from shapes, a moving diagram, or a worked numeric example.
{screen_rules(cfg)} This applies to concepts, examples and key_points (they appear on screen).

Scenes:
{json.dumps(part, ensure_ascii=False)}

Return JSON only:
{{"scenes": [{{"id": 1, "concepts": [], "examples": [], "key_points": [],
"visuals": [{{"type": "...", "description": "..."}}]}}]}}""", task="analyze")
        items = as_scenes(raw)
        by_id = {x.get("id"): x for x in items}
        for j, s in enumerate(part):
            a = by_id.get(s["id"]) or (items[j] if j < len(items) else {})
            out.append({"id": s["id"], **{k: a.get(k, []) for k in
                                          ("concepts", "examples", "key_points", "visuals")}})
    return {"scenes": out}


# --------------------------------------------------------------------------- #
# Stage 4: Manim spec - every step is pinned to a spoken sentence
# --------------------------------------------------------------------------- #
def pin_steps(steps, timeline, dur):
    """Turn Gemini's `sentence` indices into absolute seconds taken from the REAL audio."""
    n = len(timeline)
    groups = {}
    for st in steps:
        if not isinstance(st, dict):
            continue
        try:
            idx = int(st.get("sentence"))
        except (TypeError, ValueError):
            idx = None
        if idx is None or not 0 <= idx < n:
            try:   # fall back to its own at_sec, mapped onto the sentence that is spoken then
                t = float(st.get("at_sec", 0.0))
            except (TypeError, ValueError):
                t = 0.0
            idx = next((s["i"] for s in timeline if s["start"] <= t < s["end"]), n - 1)
        groups.setdefault(idx, []).append(st)
    out = []
    for idx in sorted(groups):
        seg = timeline[idx]
        m = len(groups[idx])
        gap = (seg["end"] - seg["start"]) * 0.8 / m
        for j, st in enumerate(groups[idx]):
            at = seg["start"] + 0.05 + gap * j
            out.append({**{k: v for k, v in st.items() if k != "at_sec"},
                        "sentence": idx, "at_sec": round(min(at, dur - 1.0), 2),
                        "max_run": round(max(0.4, min(1.2, gap)), 2)})
    return out


def stage_spec(gem, cfg, script, audio, analysis):
    scenes = []
    for s, a in zip(script["scenes"], analysis["scenes"]):
        au = audio[s["id"]]
        scenes.append({"id": s["id"], "heading": s["heading"], "duration_sec": round(au["duration"], 1),
                       "sentences": [{"index": t["i"], "starts_at_sec": t["start"], "text": t["text"]}
                                     for t in au["timeline"]],
                       **{k: a[k] for k in ("concepts", "examples", "key_points", "visuals")}})
    items = []
    for i in range(0, len(scenes), 3):
        raw = gem.json(f"""You are an award-winning motion-graphics designer (3Blue1Brown style) planning
RICH Manim Community Edition animations. Narration audio already exists, so timing is fixed.
Every step is pinned to a sentence ("sentence" = its index); it appears while that sentence is spoken.

DESIGN RULES
- EVERY sentence gets at least {cfg.min_steps_per_sentence} steps (3-4 for important ones). The screen
  must change constantly; nothing stays static for more than ~2 seconds.
- Explain with PICTURES, not text: analogies built from shapes (people = circles, data = dots,
  boxes = containers), arrows with dots flowing along them, graphs that draw themselves with a
  moving dot, counters that count up, before/after splits, zooming into a box to reveal its inside,
  step-by-step worked numeric examples with highlights, color-coded parts that stay the same color.
- Work in PHASES: a scene has 2-4 phases (set "phase": 1,2,3). Each phase has 4-12 objects. When
  the phase number changes, the previous phase is cleared and a new layout starts. The heading stays.
- Use the visuals list of each scene: realise ALL of them, not just the first one.
- Make motion feel PHYSICAL and alive: things enter from a direction (slide/grow with a small
  overshoot), not just appear; related objects arrive one after another (staggered), not all at once;
  arrows carry travelling dots; important numbers pulse or flash when they land; cards have a soft
  shadow; idle objects keep a tiny float so the frame never looks frozen.
- Short on-screen text only (labels, numbers); the narrator carries the explanation.
{screen_rules(cfg)}
- FRAME: {layout_rules(cfg)} Choose every "position" for THIS frame.

For each scene return:
- "id", "title_text"
- "steps": ordered list of {{"sentence": int, "phase": int,
    "action": "create|write|fade_in|grow|transform|highlight|indicate|flash|move|flow|count_up|zoom_to|fade_out",
    "object": "what it is, concretely (e.g. 5 blue dots in a column labelled 'Inputs')",
    "text": "on-screen text/numbers (plain text/unicode, no LaTeX)",
    "position": "e.g. {'top, center, left third, below previous' if cfg.is_wide else 'top, middle, bottom third, below previous'}"}}

Scenes:
{json.dumps(scenes[i:i + 3], ensure_ascii=False)}

Return JSON only: {{"scenes": [...]}}""", task="spec")
        items += as_scenes(raw)
    by_id = {x.get("id"): x for x in items if isinstance(x, dict)}
    out = []
    for i, sc in enumerate(scenes):
        sp = by_id.get(sc["id"]) or (items[i] if i < len(items) else {})
        tl = audio[sc["id"]]["timeline"]
        d = audio[sc["id"]]["duration"]
        out.append({"id": sc["id"], "heading": sc["heading"],
                    "title_text": sp.get("title_text") or sc["heading"],
                    "steps": pin_steps(sp.get("steps") or [], tl, d),
                    "key_points": sc["key_points"], "visuals": sc["visuals"],
                    "duration_sec": round(d, 2), "timeline": tl})
    return {"scenes": out}


# --------------------------------------------------------------------------- #
# Stage 5: Manim code (Gemini) + render with auto-repair
# --------------------------------------------------------------------------- #
def layout_rules(cfg):
    """Frame facts + layout advice for the chosen aspect (the frame is NOT always 16:9)."""
    fw, fh = cfg.frame_size
    if cfg.is_wide:
        return f"{cfg.aspect} wide frame, {fw:.2f} x {fh:.2f} scene units. Side-by-side layouts work well."
    return (f"{cfg.aspect} {'vertical' if fh > fw else 'square'} frame, only {fw:.2f} wide x {fh:.2f} tall scene units "
            "(config.frame_width / config.frame_height). Stack things TOP TO BOTTOM (VGroup(...).arrange(DOWN)); "
            "at most 2 items side by side, never 3 or more in a row. Text is BIG: font_size 34-56, short lines "
            "(<= 4 words, break longer text into several Text objects stacked in a VGroup). Use the full height: "
            "heading at the top, the main visual in the middle, labels/captions below it. Never place anything at a fixed "
            "x beyond +-3.5 or a width above 7; use config.frame_width / config.frame_height instead of numbers.")


def manim_rules(cfg, cls, dur):
    if cfg.screen_language == "ur":
        text_rule = (f"- On-screen text is Urdu (right-to-left). Every Text(...) that contains Urdu must pass font=\"{cfg.urdu_font}\". "
                     "Do NOT use Tex/MathTex. Use FadeIn (not Write) for Urdu text. Center it or "
                     "align it to the right; keep lines short (<= 6 words) so they fit the frame. A label that is only an "
                     "English term, a number or a formula is its own Text(...) WITHOUT the Urdu font (default font); never "
                     "mix English and Urdu letters inside one Text(...).")
    elif cfg.allow_latex:
        text_rule = "- You may use MathTex/Tex sparingly for equations."
    else:
        text_rule = "- Do NOT use Tex, MathTex or any LaTeX. Use Text() with unicode (², √, π, ≈, →)."
    return f"""Rules:
- Manim Community Edition (v0.18+). `from manim import *` and `SyncScene` are already imported.
- Define exactly one class `{cls}(SyncScene)` with a `construct` method.
- SYNC: before each spec step call `self.at(<at_sec>)` with the exact value. A step's animation
  run_time must be <= that step's "max_run". End with `self.finish({dur})`. Never hard-code long waits.
- Implement EVERY step of the spec (do not skip any). Use LaggedStart / AnimationGroup to play
  several objects of one step together.
- PHASES: keep `title` (heading at top). When "phase" changes, call
  `self.new_phase(keep=[title])` right after `self.at(...)`, then build the new layout.
- CREATIVITY (this is what makes it look great): use the colour palette PAL["blue"|"green"|"orange"|
  "pink"|"yellow"|"purple"|"white"] and keep one colour per concept for the whole scene; rounded
  rectangles (RoundedRectangle) with fill_opacity for cards; Arrow + a Dot travelling along it with
  MoveAlongPath for data flow; ValueTracker + always_redraw(lambda: Text(f"{{vt.get_value():.0f}}"))
  for counters; axes.plot(...) with a Dot following the curve; Indicate / Flash / Circumscribe to
  highlight; self.zoom_to(mob) / self.reset_cam() to zoom into details; GrowFromCenter, GrowArrow,
  ReplacementTransform for morphing; small icons built from Circle/Square/Triangle/Line.
- REALISM (this is what separates a slideshow from a real animation):
  * Easing: never leave the default linear feel. Pop-ins / entrances use rate_func=rate_functions.ease_out_back
    (small overshoot); moves use rate_functions.ease_in_out_cubic; arrivals use ease_out_cubic;
    attention pulses use rate_functions.there_and_back. Keep every run_time <= the step's max_run.
  * Entrances have DIRECTION: FadeIn(mob, shift=UP*0.3) / shift=LEFT*0.4 etc., or GrowFromCenter /
    GrowFromPoint, never a bare FadeIn for important objects. Groups use LaggedStart(lag_ratio=0.12-0.25).
  * Weight + depth: put a soft shadow behind cards (same RoundedRectangle, BLACK, fill_opacity=0.35,
    stroke_width=0, shifted DOWN*0.06+RIGHT*0.04, drawn first). Give key shapes a glow: a copy with
    stroke_width=12, stroke_opacity=0.18 behind it. Use gradient fills (set_color_by_gradient /
    fill_color + a lighter stroke) instead of flat colours.
  * Life while waiting: add a gentle idle motion to 1-3 hero objects with an updater, e.g.
    m.add_updater(lambda o, dt: o.shift(UP * 0.003 * np.sin(self.renderer.time * 2))) - tiny amplitude,
    and call m.clear_updaters() before FadeOut / Transform of that object.
  * Impact: when a number or dot lands, add Flash(...) or a quick Indicate(scale_factor=1.15);
    moving dots leave a short TracedPath trail (stroke_width=2, dissipating_time=0.6) when it is cheap.
  * Subtle background life: ONE faint NumberPlane/grid or a few tiny slow-drifting dots at opacity <= 0.12;
    never let it compete with the content.
  * Squash & settle: for bouncy arrivals use rate_functions.ease_out_bounce sparingly (at most once a scene).
- Allowed API: rate_functions (ease_*, there_and_back, smooth), TracedPath, GrowFromPoint, Create, Write, FadeIn, FadeOut, Transform, ReplacementTransform, GrowFromCenter,
  GrowArrow, Indicate, Flash, Circumscribe, LaggedStart, AnimationGroup, Succession, MoveAlongPath,
  Rotate, ApplyWave, ShowPassingFlash, ValueTracker, always_redraw, Axes, NumberPlane, NumberLine,
  Rectangle, RoundedRectangle, Circle, Square, Triangle, Polygon, Arrow, DoubleArrow, Line,
  DashedLine, Arc, VGroup, Dot, Text, Brace, SurroundingRectangle.
- NO-LaTeX safety: never use include_numbers=True, add_coordinates, get_axis_labels,
  get_graph_label, Brace.get_text, DecimalNumber, Integer, Table, Matrix, BarChart. Make labels with Text().
{text_rule}
- No external files, images, SVGs or fonts. FRAME: {layout_rules(cfg)} Keep everything inside it using
  .scale_to_fit_width(), .next_to(), .to_edge(), VGroup(...).arrange(). {"Text font_size 24-44." if cfg.is_wide else "Text font_size 34-56."}
- Return ONLY Python code, no explanation, no markdown fences."""


def render_scene(gem, cfg, sp, work):
    sid = sp["id"]
    cls = f"Scene{sid:02d}"
    code_dir, media_dir = work / "manim", (work / "media").resolve()
    code_dir.mkdir(exist_ok=True)
    py = (code_dir / f"scene_{sid:02d}.py").resolve()
    rules = manim_rules(cfg, cls, sp["duration_sec"])
    spec = {k: sp[k] for k in ("id", "title_text", "duration_sec", "steps", "key_points", "visuals")}
    spec["sentences"] = [{"index": t["i"], "start": t["start"], "end": t["end"]} for t in sp["timeline"]]
    code = gem.code("Write Manim code for this scene specification.\n\n"
                    f"{json.dumps(spec, ensure_ascii=False, indent=1)}\n\n{rules}", task="code")
    err = ""
    for attempt in range(cfg.retries + 1):
        code, err = precheck(code, cls, cfg)
        if not err:
            write_scene_file(py, code, code_dir, cfg)
            video, err = render_manim(py, cls, media_dir, cfg.quality)
            if video:
                return video
        log.warning("scene %s attempt %d failed: %s", sid, attempt + 1, err.strip()[-300:])
        if attempt < cfg.retries:
            code = gem.code(f"This Manim code failed.\n\nCODE:\n{code}\n\nERROR (tail):\n"
                            f"{err[-3000:]}\n\nFix it and keep the class name {cls}(SyncScene) "
                            f"and the self.at(...) / self.finish(...) calls.\n\n{rules}",
                            temperature=0.2, task="repair")
    log.warning("scene %s: using fallback title card", sid)
    write_scene_file(py, fallback_code(cfg, sp, cls), code_dir, cfg)
    video, err = render_manim(py, cls, media_dir, cfg.quality)
    if not video:
        raise RuntimeError(f"scene {sid} failed even with fallback:\n{err[-1500:]}")
    return video


def stage_manim(gem, cfg, spec, work):
    path = work / "5_clips.json"
    clips = load_json(path, {})
    for sp in spec["scenes"]:
        key = str(sp["id"])
        if key in clips and Path(clips[key]).exists():
            log.info("  scene %s cached", key)
            continue
        log.info("  scene %s: generating + rendering", key)
        clips[key] = str(render_scene(gem, cfg, sp, work))
        save_json(path, clips)       # per-scene resume
    return {int(k): v for k, v in clips.items()}


# --------------------------------------------------------------------------- #
# Stage 6: sound effects - narration + SFX mix per scene (no Gemini, instant)
# --------------------------------------------------------------------------- #
def stage_sfx(cfg, spec, audio, work):
    """Build audio/mix_scene_XX.wav = narration + SFX pinned to the spec steps.

    Returns {scene_id: Path(mix wav)}. Use these files instead of the plain narration wavs when
    the final video is assembled. Tunables (optional cfg attrs): sfx_level (default 0.22 = SFX peak
    relative to the voice), sfx_ambient (default True = very quiet background pad).
    """
    audio_dir = work / "audio"
    audio_dir.mkdir(exist_ok=True)
    info_path = work / "6_sfx.json"
    info = load_json(info_path, {})
    level = float(getattr(cfg, "sfx_level", 0.22))
    ambient = bool(getattr(cfg, "sfx_ambient", True))
    out = {}
    for sp in spec["scenes"]:
        sid = sp["id"]
        nar = Path(audio[sid]["file"])
        mix = audio_dir / f"mix_scene_{sid:02d}.wav"
        sig = hashlib.md5(json.dumps([sp["steps"], audio[sid]["sig"], level, ambient],
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if info.get(str(sid)) == sig and mix.exists() and mix.stat().st_size > 0:
            out[sid] = mix
            continue
        build_scene_mix(nar, sp["steps"], mix, level=level, ambient=ambient, seed=sid)
        info[str(sid)] = sig
        save_json(info_path, info)
        log.info("  scene %s: sfx mixed (%d steps)", sid, len(sp["steps"]))
        out[sid] = mix
    return out
