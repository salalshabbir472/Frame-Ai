"""
scene_plan.py - decides what happens to the scenes the user storyboarded in the frontend.

The user hands over M scenes (a description, or their own Manim / Remotion file). The video can carry only so many
scenes: too few and every scene drags on, too many and the screen flickers. This module

  1. BUDGET    works out how many scenes this video can carry (length, `scene_sec`, `scene_min_sec`, `max_scenes`)
  2. FIT       too FEW scenes  -> Frame invents the missing ones (a hook, a bridge, a worked example, a recap ...)
               too MANY scenes -> the least important ones are dropped; a dropped description is FOLDED into its
                                  neighbour ("also cover: ..."), so no idea is lost. Own-file scenes are protected.
               scene_fit = "exact" turns this off: your list is used as it is (only the hard limits cut it).
  3. PLACE     decides WHERE every scene goes
                 prompt  : Frame writes the script around the storyboard (one scene = one script scene, in your order)
                 script  : the finished narration is cut into consecutive parts and Gemini matches every storyboard
                           scene to the part it illustrates; leftover parts become "free" scenes Frame designs itself
  4. REPORT    "<video>.scenes.json": what was kept / added / dropped / folded and which engine drew every scene

Every decision is cached in build/<slug>/1a_scene_plan.json, so a quota wall never changes the plan between runs.

A plan entry:  {"origin": "user" | "generated" | "free", "ref": <order of the user's scene or None>,
                "title", "description", "mode": "auto" | "file", "engine", "file_name", "folded": [...]}
"""
from __future__ import annotations

import ast
import json
import logging
import math
import re
import zipfile
from pathlib import Path

from config import LANGS, as_scenes, load_json, save_json
from gemini_client import QuotaExhausted
from scene_input import has_file

log = logging.getLogger("edu")

FOLD_CHARS = 300          # how much of a dropped scene is folded into its neighbour
DIGEST_CHARS = 600


# --------------------------------------------------------------------------- #
# 0. A peek into the user's own animation code (cheap, no Gemini): what does it put on screen?
#    The narrator is then written to match it, and the alignment can match it to the right sentences.
# --------------------------------------------------------------------------- #
_CSS_WORDS = {"flex", "center", "absolute", "relative", "column", "row", "bold", "normal", "none", "auto", "hidden", "solid",
              "block", "inline", "grid", "wrap", "left", "right", "top", "bottom", "middle", "pointer", "ease", "linear",
              "inherit", "transparent", "white", "black", "uppercase", "lowercase", "italic", "visible", "clamp", "extend",
              "identity", "react", "remotion", "default", "number", "string", "boolean", "utf-8", "rtl", "ltr", "svg",
              "circle", "rect", "path", "line", "text", "div", "span", "Inter", "Arial", "sans-serif", "monospace"}
_JS_STR = re.compile(r"""(?:"([^"\n\\]{3,80})"|'([^'\n\\]{3,80})'|`([^`\n\\$]{3,80})`)""")
_JSX_TEXT = re.compile(r">([^<>{}\n]{3,80})<")


def _looks_like_label(s: str) -> bool:
    s = s.strip()
    if len(s) < 3 or not re.search(r"[A-Za-z؀-ۿ]{2}", s) or s.lower() in _CSS_WORDS:
        return False
    if re.fullmatch(r"[#.\w/:%-]+", s) and " " not in s and not s[0].isupper():
        return False                                    # css value, path, identifier
    if re.search(r"=>|\bpx\b|rgba?\(|#[0-9a-fA-F]{3,8}\b|^\.{0,2}/|\.(tsx?|jsx?|css|png|svg)$|^\d+(\.\d+)?(px|s|ms|%)$", s):
        return False
    return True


def _zip_texts(path: Path, limit=120_000) -> str:
    out, used = [], 0
    try:
        with zipfile.ZipFile(path) as z:
            for i in z.infolist():
                n = i.filename
                if i.is_dir() or "node_modules/" in n or not n.lower().endswith((".tsx", ".jsx", ".ts", ".js")) or i.file_size > 400_000:
                    continue
                out.append(z.read(i).decode("utf-8", "replace"))
                used += i.file_size
                if used > limit:
                    break
    except (zipfile.BadZipFile, OSError):
        return ""
    return "\n".join(out)


def code_digest(path, engine: str = "", limit: int = DIGEST_CHARS) -> str:
    """One line describing what the user's own scene file shows: its texts, comments and scene names."""
    try:
        p = Path(path)
        text = _zip_texts(p) if p.suffix.lower() == ".zip" else p.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        return ""
    labels, notes, names = [], [], []

    def add(s):
        s = " ".join(str(s).split())
        if s and _looks_like_label(s) and s not in labels:
            labels.append(s)

    if (engine or "").lower() == "manim" or str(path).lower().endswith(".py"):
        try:
            tree = ast.parse(text)
            doc = ast.get_docstring(tree)
            if doc:
                notes.append(" ".join(doc.split())[:200])
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    names.append(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", node.name))
                    d = ast.get_docstring(node)
                    if d:
                        notes.append(" ".join(d.split())[:200])
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "\n" not in node.value.strip():
                    add(node.value)
        except SyntaxError:
            for m in re.finditer(r"""["']([^"'\n]{3,80})["']""", text):
                add(m.group(1))
        notes += [c.strip("# ").strip() for c in re.findall(r"(?m)^\s*#\s*(.{8,120})$", text)][:4]
    else:
        for m in _JSX_TEXT.finditer(text):
            add(m.group(1))
        for m in _JS_STR.finditer(text):
            add(next(g for g in m.groups() if g))
        notes += [c.strip("/* ").strip() for c in re.findall(r"(?m)^\s*//\s*(.{8,120})$", text)][:4]
    parts = []
    if names:
        parts.append("scene: " + ", ".join(dict.fromkeys(names))[:80])
    if labels:
        parts.append("texts shown: " + "; ".join(f"“{x}”" for x in labels[:20]))
    if notes:
        parts.append("notes: " + " | ".join(dict.fromkeys(n for n in notes if n))[:200])
    return ("The user's own animation. " + ". ".join(parts))[:limit] if parts else ""


# --------------------------------------------------------------------------- #
# 1. Budget: how many scenes can this video carry?
# --------------------------------------------------------------------------- #
def scene_budget(cfg, est_sec: float, max_units: int | None = None) -> dict:
    """lo / target / up / hi. `target` is what scene_sec asks for, [lo, up] is the pacing the plan accepts as it is
    (a scene may last 1.5x longer or 1.5x shorter than scene_sec), `hi` is the physical limit."""
    est = max(5.0, float(est_sec))
    scene_sec, min_sec = max(3.0, float(cfg.scene_sec)), max(2.0, float(cfg.scene_min_sec))
    hi = max(1, min(int(cfg.max_scenes), int(est // min_sec) or 1))
    if max_units:
        hi = max(1, min(hi, int(max_units)))
    target = max(1, min(hi, round(est / scene_sec)))
    lo = max(1, min(target, math.ceil(est / (scene_sec * 1.5))))
    up = max(target, min(hi, math.ceil(target * 1.5)))
    return {"est_sec": round(est, 1), "lo": lo, "target": target, "up": up, "hi": hi}


def wanted_count(m: int, b: dict, policy: str) -> int:
    if policy == "exact":
        return min(m, b["hi"])
    if m < b["lo"]:
        return b["lo"]
    return min(m, b["up"])


# --------------------------------------------------------------------------- #
# 2. Fit: drop (and fold) the least important scenes
# --------------------------------------------------------------------------- #
def _importance(scenes, i, dropped) -> float:
    s, m = scenes[i], len(scenes)
    sc = 100.0 if has_file(s) else 0.0                                  # the user's own code is never the first to go
    sc += min(len(s["description"]), 600) / 600 * 10 + (2 if s["title"] else 0)
    if i == 0:
        sc += 40                                                        # the hook
    if i == m - 1:
        sc += 30                                                        # the closing scene
    sc += 15 * sum(1 for j in (i - 1, i + 1) if j in dropped)           # do not wipe out a whole stretch of the storyboard
    return sc - i * 1e-6                                                # a tie: the later scene goes first


def choose_drops(scenes, n_drop: int) -> set:
    dropped: set = set()
    for _ in range(max(0, min(n_drop, len(scenes)))):
        pick = min((i for i in range(len(scenes)) if i not in dropped), key=lambda i: _importance(scenes, i, dropped))
        dropped.add(pick)
    return dropped


def _entry(c: dict, origin="user") -> dict:
    return {"origin": origin, "ref": c.get("order"), "title": c.get("title", ""), "description": c.get("description", ""),
            "mode": "file" if has_file(c) else "auto", "engine": c.get("engine", "") if has_file(c) else "",
            "file_name": c.get("file_name", "") if has_file(c) else "", "folded": []}


def fit_scenes(scenes, k: int):
    """Keep k of the user's scenes. Returns (entries, dropped_report). A dropped description is folded into the nearest
    kept description scene."""
    drops = choose_drops(scenes, len(scenes) - k) if k < len(scenes) else set()
    entries = [_entry(c) for i, c in enumerate(scenes) if i not in drops]
    kept_idx = [i for i in range(len(scenes)) if i not in drops]
    report = []
    for d in sorted(drops):
        c = scenes[d]
        text = " ".join(x for x in (c["title"], c["description"]) if x).strip()
        into = None
        if text and not has_file(c):
            order = sorted(range(len(kept_idx)), key=lambda p: (abs(kept_idx[p] - d), kept_idx[p] > d))     # nearest, the earlier one first
            for p in order:
                e = entries[p]
                if e["mode"] == "auto":
                    note = f"{c['title']}: {c['description']}" if c["title"] and c["description"] else text
                    e["description"] = (e["description"] + f"\nAlso cover briefly: {note[:FOLD_CHARS]}").strip()[:4000]
                    e["folded"].append(c["title"] or text[:40])
                    into = e["ref"]
                    break
        report.append({"order": c["order"], "title": c["title"] or c["description"][:40], "file": has_file(c),
                       "folded_into": into})
    return entries, report


# --------------------------------------------------------------------------- #
# 3a. Generate the missing scenes (prompt videos: Frame writes the script around the storyboard)
# --------------------------------------------------------------------------- #
def propose_extra(gem, cfg, entries, g: int):
    from stages_gemini import screen_rules
    board = []
    for i, e in enumerate(entries, 1):
        what = e["description"][:220] or "(no text)"
        if e["mode"] == "file":
            what = "THE USER'S OWN ANIMATION, do not change it. " + (e["description"][:120] or e.get("digest", "")[:200])
        board.append({"n": i, "title": e["title"], "scene": what})
    raw = gem.json(f"""You are a teacher finishing the STORYBOARD of a narrated explainer video.
Topic: {cfg.topic}. Audience: {cfg.audience}. About {cfg.minutes:g} minutes.
{('User instruction: ' + cfg.custom_instruction) if cfg.custom_instruction else ''}
The user already drew these {len(entries)} scenes, in this order (they stay exactly as they are):
{json.dumps(board, ensure_ascii=False)}
The video needs {g} MORE scene(s) so that no scene drags on. Invent exactly {g} new scene(s) that a great teacher would add:
a hook at the very start if scene 1 is not one, a recap at the end if the last scene is not one, a bridge (analogy, tiny example,
"why this matters") where the jump between two scenes is big, a worked number example for the hardest idea.
Never repeat an existing scene, stay on the topic, spread the new scenes over the whole video (never more than 2 in a row).
Every new scene must be something that can be DRAWN. {screen_rules(cfg)}
"after" = the number of the storyboard scene it comes after (0 = before scene 1, {len(entries)} = after the last one).
Return JSON only: {{"scenes": [{{"after": 0, "title": "2-5 words", "description": "1-2 sentences: what is shown, as a picture"}}]}}""",
                   temperature=0.8, task="outline")
    out = []
    for x in as_scenes(raw):
        try:
            after = max(0, min(len(entries), int(x.get("after"))))
        except (TypeError, ValueError):
            continue
        t, d = str(x.get("title") or "").strip()[:120], str(x.get("description") or "").strip()[:600]
        if t or d:
            out.append({"after": after, "title": t, "description": d})
    return out[:g]


def insert_extra(entries, extra):
    out, by = [], {}
    for x in extra:
        by.setdefault(x["after"], []).append(x)
    for x in by.get(0, []):
        out.append({"origin": "generated", "ref": None, "title": x["title"], "description": x["description"], "mode": "auto",
                    "engine": "", "file_name": "", "folded": []})
    for i, e in enumerate(entries, 1):
        out.append(e)
        for x in by.get(i, []):
            out.append({"origin": "generated", "ref": None, "title": x["title"], "description": x["description"], "mode": "auto",
                        "engine": "", "file_name": "", "folded": []})
    return out


def _attach_digests(cfg, entries):
    refs = {c["order"]: c for c in cfg.custom_scenes}
    for e in entries:
        c = refs.get(e.get("ref"))
        if e["mode"] == "file" and c:
            e["digest"] = code_digest(c["local_path"], c.get("engine", ""))


def plan_prompt_scenes(gem, cfg) -> dict:
    """Prompt video. -> {"entries": [...], "report": {...}} (the script is then written scene by scene around the entries)."""
    scenes = cfg.custom_scenes
    b = scene_budget(cfg, cfg.minutes * 60)
    k = wanted_count(len(scenes), b, cfg.scene_fit)
    entries, dropped = fit_scenes(scenes, k)
    _attach_digests(cfg, entries)
    made, note = 0, ""
    if k > len(entries):
        want = k - len(entries)
        try:
            extra = propose_extra(gem, cfg, entries, want)
            entries, made = insert_extra(entries, extra), len(extra)
            if made < want:
                note = f"Gemini proposed {made} of {want} wanted scenes"
        except QuotaExhausted:
            raise
        except Exception as e:  # noqa: BLE001
            note = f"could not invent extra scenes ({str(e)[:100]}); your own scenes are used as they are"
            log.warning("  scenes: %s", note)
    log.info("  scenes: you gave %d; this video fits %d-%d (target %d) -> keeping %d, dropped %d, added %d",
             len(scenes), b["lo"], b["up"], b["target"], len(entries) - made, len(dropped), made)
    return {"entries": entries, "report": {"mode": "prompt", "requested": len(scenes), "final": len(entries), "kept": len(entries) - made,
                                            "generated": made, "free": 0, "dropped": dropped, "budget": b, "policy": cfg.scene_fit, "note": note}}


# --------------------------------------------------------------------------- #
# 3b. Place the scenes on a finished narration (own script / source text)
# --------------------------------------------------------------------------- #
def _words(s: str) -> int:
    return max(1, len(s.split()))


def even_starts(sents, k: int):
    """First sentence of each of k consecutive parts of about equal length (in words). Every part has >= 1 sentence."""
    n = len(sents)
    k = max(1, min(k, n))
    w = [_words(s) for s in sents]
    total, acc, starts = float(sum(w)), 0.0, [0]
    mids = []
    for x in w:
        mids.append(acc + x / 2)
        acc += x
    for j in range(1, k):
        want = total * j / k
        i = next((i for i, m in enumerate(mids) if m >= want), n - 1)
        starts.append(min(max(i, starts[-1] + 1), n - (k - j)))
    return starts


def _free_entry():
    return {"origin": "free", "ref": None, "title": "", "description": "", "mode": "auto", "engine": "", "file_name": "", "folded": []}


def _interleave(users, k: int):
    """k entries: the user's scenes spread evenly, the gaps are free scenes."""
    m = len(users)
    slots = {int((i + 0.5) * k / m): users[i] for i in range(m)} if m else {}
    return [slots.get(j) or _free_entry() for j in range(k)]


def _align_prompt(cfg, sents, users, free, hi):
    words = [_words(s) for s in sents]
    target_words = max(8, int(LANGS[cfg.language]["wps"] * cfg.scene_sec))
    board = []
    for i, e in enumerate(users, 1):
        what = e["description"][:260]
        if e.get("digest"):
            what = (what + " " + e["digest"][:300]).strip()
        board.append({"n": i, "title": e["title"], "scene": what or "(no text)"})
    numbered = "\n".join(f"[{i}] ({words[i]}w) {s[:150]}" for i, s in enumerate(sents))
    return f"""You are the editor of a narrated video. The narration is finished (numbered sentences, with their word counts).
The user also drew a STORYBOARD. Cut the narration into consecutive segments and say which storyboard scene is on screen during each.
STORYBOARD ({len(users)} scenes, fixed order): {json.dumps(board, ensure_ascii=False)}
RULES
- The segments cover the WHOLE narration in order, no gaps. A segment is given by the index of its first sentence; the first starts at 0.
- Every storyboard scene is used EXACTLY once and in the storyboard's order. Its segment is the part of the narration that this
  scene illustrates: match meaning, titles, and the texts the scene shows.
- Parts of the narration that no storyboard scene illustrates become segments with "scene": 0 (Frame designs those itself). Use {free}
  such segment(s) (a little fewer or more is fine, but never more than {max(0, hi - len(users))}).
- Pace: a segment is about {target_words} words long (never shorter than {max(4, target_words // 3)} words); a long idea may take more.
Return JSON only: {{"segments": [{{"scene": 1, "start": 0}}, {{"scene": 0, "start": 12}}]}}
NARRATION
{numbered}"""


def _check_segments(raw, n_sent, m, hi, words, min_words):
    """-> (list of (scene_no, start), '') or (None, why)."""
    segs = []
    for x in (raw.get("segments") if isinstance(raw, dict) else raw) or []:
        try:
            segs.append((int(x.get("scene") or 0), int(x.get("start"))))
        except (AttributeError, TypeError, ValueError):
            return None, "every segment needs integer scene and start"
    if not segs:
        return None, "no segments"
    segs[0] = (segs[0][0], 0)
    if any(b[1] <= a[1] for a, b in zip(segs, segs[1:])) or segs[-1][1] >= n_sent:
        return None, "starts must strictly increase and stay below the number of sentences"
    if [s for s, _ in segs if s] != list(range(1, m + 1)):
        return None, f"storyboard scenes must appear once each, in order 1..{m}"
    if len(segs) > hi:
        return None, f"at most {hi} segments"
    out = []                                                           # free segments that are too short are merged away
    for i, (sc, st) in enumerate(segs):
        end = segs[i + 1][1] if i + 1 < len(segs) else n_sent
        if sc == 0 and out and sum(words[st:end]) < min_words:
            continue
        out.append((sc, st))
    return out, ""


def plan_script_scenes(gem, cfg, sents, work=None) -> dict:
    """Own script / source text. sents = all sentences in order. -> {"entries", "starts", "report"}: entry j covers
    sents[starts[j]:starts[j+1]]."""
    wps = LANGS[cfg.language]["wps"]
    est = sum(_words(s) for s in sents) / wps
    scenes = cfg.custom_scenes
    b = scene_budget(cfg, est, max_units=len(sents))
    k = wanted_count(len(scenes), b, cfg.scene_fit)
    users, dropped = fit_scenes(scenes, k)
    _attach_digests(cfg, users)
    free = max(0, k - len(users)) if cfg.scene_fit != "exact" else 0
    total = len(users) + free
    words = [_words(s) for s in sents]
    min_words = max(4, int(wps * cfg.scene_min_sec * 0.6))
    segs, how, note = None, "even", ""
    if users:
        why = ""
        for attempt in range(2):
            try:
                prompt = _align_prompt(cfg, sents, users, free, b["hi"])
                if why:
                    prompt += f"\n\nYOUR PREVIOUS ANSWER WAS REJECTED: {why}. Fix it."
                raw = gem.json(prompt, temperature=0.2, task="outline")
            except QuotaExhausted:
                raise
            except Exception as e:  # noqa: BLE001
                why = str(e)[:100]
                note = f"matching with Gemini failed ({why})"
                break
            segs, why = _check_segments(raw, len(sents), len(users), b["hi"], words, min_words)
            if segs:
                how = "gemini"
                break
            note = f"Gemini's matching was rejected ({why})"
    if segs:
        entries, starts, it = [], [], iter(users)
        for sc, st in segs:
            entries.append(next(it) if sc else _free_entry())
            starts.append(st)
    else:
        if users:
            log.warning("  scenes: %s: spreading your scenes evenly over the narration instead", note or "no matching")
        entries = _interleave(users, total)
        starts = even_starts(sents, len(entries))
        entries = entries[:len(starts)]
    nfree = sum(1 for e in entries if e["origin"] == "free")
    log.info("  scenes: %d given -> %d kept, %d dropped, %d free part(s); narration cut into %d scenes (%s)",
             len(scenes), len(users), len(dropped), nfree, len(entries), how)
    return {"entries": entries, "starts": starts,
            "report": {"mode": "script", "requested": len(scenes), "final": len(entries), "kept": len(users), "generated": 0,
                       "free": nfree, "dropped": dropped, "budget": b, "policy": cfg.scene_fit, "placed_by": how, "note": note}}


# --------------------------------------------------------------------------- #
# 4. Shared helpers for the stages + the report
# --------------------------------------------------------------------------- #
def idea_of(e: dict) -> str:
    """The 'idea' line the narration writer sees for this entry."""
    parts = [e.get("description", "").strip()]
    if e.get("mode") == "file":
        parts.append(e.get("digest") or "The user's own animation file is shown on screen as it is.")
    return " ".join(p for p in parts if p).strip()[:900] or e.get("title", "")


def public_plan(e: dict) -> dict:
    """What travels with a script scene (no machine paths, nothing large)."""
    return {k: e.get(k) for k in ("origin", "ref", "title", "description", "mode", "engine", "file_name", "folded")}


def build_report(cfg, script, work: Path, meta: dict | None = None) -> dict:
    used = load_json(Path(work) / "5_engines.json", {}) or {}
    rows = []
    for s in script["scenes"]:
        p = s.get("plan") or {}
        u = used.get(str(s["id"]), {})
        eng = u.get("engine", "")
        row = {"scene": s["id"], "heading": s["heading"], "origin": p.get("origin", "frame"), "from_your_scene": p.get("ref"),
               "engine": eng}
        if p.get("mode") == "file":
            row["own_file"] = p.get("file_name", "")
            if not str(eng).startswith("own-"):
                row["note"] = f"your file could not be used ({u.get('own_error') or 'see the log'}); Frame built this scene instead"
        if p.get("folded"):
            row["folded"] = p["folded"]
        rows.append(row)
    return {"title": cfg.topic, "plan": meta or {}, "scenes": rows}


def summary_line(rep: dict) -> str:
    p = rep.get("plan") or {}
    if not p:
        return ""
    own = sum(1 for r in rep["scenes"] if str(r.get("engine", "")).startswith("own-"))
    bits = [f"{p.get('final', len(rep['scenes']))} scenes from your {p.get('requested', 0)}"]
    if own:
        bits.append(f"{own} your own file")
    if p.get("generated"):
        bits.append(f"{p['generated']} added by Frame")
    if p.get("free"):
        bits.append(f"{p['free']} free")
    if p.get("dropped"):
        bits.append(f"{len(p['dropped'])} dropped")
    bad = sum(1 for r in rep["scenes"] if r.get("note"))
    if bad:
        bits.append(f"{bad} own file(s) failed, rebuilt by Frame")
    return "Scenes: " + ", ".join(bits)
