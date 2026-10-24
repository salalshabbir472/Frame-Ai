"""
remotion_stage.py - Remotion (React/TSX) visuals for the narrated pipeline, plus the per-scene ENGINE ROUTER.

  stage 4b  stage_route    -> Gemini decides, scene by scene, "remotion" or "manim" (Remotion is the default)
  stage 5   stage_visuals  -> renders every scene with its engine:
                                remotion scene: Gemini-written TSX pinned to the spoken sentences (tsc + render + repair)
                                manim scene   : the old stages_gemini.render_scene (unchanged)
                              If an engine fails after its repair attempts the scene falls back to the other engine,
                              and finally to a plain title card, so one bad scene never kills the video.

The narration sync is unchanged: every clip is handed to media.stage_assemble, which freezes / pads
each clip to the real audio length. Reuses the Remotion project helpers of remotion_video.py.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from config import as_scenes, cached, load_json, save_json
from remotion_video import (SceneError, ensure_project, extract_code, find_node, validate_tsx,
                            render_scene as remotion_render)
from stages_gemini import render_scene as manim_render, screen_rules
from user_scenes import UserSceneError, own_engines, own_scene, render_user_scene

log = logging.getLogger("edu")

FPS = 30
ENGINES = ("remotion", "manim")
BG = "#0E1024"
PAL = {"blue": "#4FC3F7", "green": "#81C784", "orange": "#FFB74D", "pink": "#F06292",
       "yellow": "#FFF176", "purple": "#BA68C8", "white": "#ECEFF1"}


# --------------------------------------------------------------------------- #
# Which engines can run on this machine?
# --------------------------------------------------------------------------- #
class Env:
    """Cached answer to 'is Node / Manim available?' (+ the npx path and the shared Remotion project)."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.npx = None
        self.project = Path(cfg.workdir) / "remotion_project"    # shared by all videos: npm install runs once
        self.can = {"manim": bool(shutil.which("manim")), "remotion": False}
        if cfg.engine != "manim" or "remotion" in own_engines(cfg):   # the user's own Remotion file needs Node even when the AI is told "manim"
            try:
                npm, self.npx = find_node()
                ensure_project(self.project, npm)
                self.can["remotion"] = True
            except SystemExit as e:
                log.warning("Remotion unavailable: %s", e)
            except Exception as e:  # noqa: BLE001
                log.warning("Remotion setup failed (%s); scenes will use Manim", str(e)[:200])
        if cfg.engine in ENGINES and not self.can[cfg.engine]:
            other = "manim" if cfg.engine == "remotion" else "remotion"
            if self.can[other]:
                log.warning("engine=%s is not available here: using %s instead", cfg.engine, other)
            else:
                raise SystemExit("Neither Remotion (Node 18+) nor Manim is available.")
        if not any(self.can.values()):
            raise SystemExit("Neither Remotion (Node 18+) nor Manim is available.")

    def usable(self, engine):
        return self.can.get(engine, False)


# --------------------------------------------------------------------------- #
# Stage 4b: Gemini picks the engine of every scene
# --------------------------------------------------------------------------- #
def _scene_digest(sp):
    objs = [str(s.get("object", ""))[:70] for s in sp["steps"][:8] if isinstance(s, dict)]
    vis = [f"{v.get('type')}: {str(v.get('description', ''))[:90]}" for v in (sp.get("visuals") or [])
           if isinstance(v, dict)][:7]
    return {"id": sp["id"], "heading": sp["heading"], "visuals": vis, "objects": objs}


def stage_route(gem, cfg, spec, env: Env, work: Path):
    """-> {scene_id: 'remotion' | 'manim'}  (cached in 4b_route.json)."""
    ids = [sp["id"] for sp in spec["scenes"]]
    # the user's own files choose their engine themselves; if such a file fails, the same engine builds the fallback
    own = {sp["id"]: sp["custom"]["engine"] for sp in spec["scenes"]
           if (sp.get("custom") or {}).get("mode") == "file" and sp["custom"].get("engine") in ENGINES}
    ai = [sp for sp in spec["scenes"] if sp["id"] not in own]
    base = _route_ai(gem, cfg, ai, [sp["id"] for sp in ai], env, work)
    return {**{i: base.get(i, "remotion") for i in ids}, **own}


def _route_ai(gem, cfg, scenes, ids, env: Env, work: Path):
    if not ids:
        return {}
    if cfg.engine in ENGINES:
        forced = cfg.engine if env.usable(cfg.engine) else next(e for e in ENGINES if env.usable(e))
        return {i: forced for i in ids}
    if not env.usable("manim"):
        return {i: "remotion" for i in ids}
    if not env.usable("remotion"):
        return {i: "manim" for i in ids}

    def ask():
        out = {}
        digests = [_scene_digest(sp) for sp in scenes]
        for i in range(0, len(digests), 8):
            raw = gem.json(f"""You choose the animation ENGINE for each scene of an educational video.
Two engines exist:
- "remotion": React + SVG/CSS motion graphics. BEST for: flow charts, process / pipeline diagrams, data flow with moving
  dots, cards and UI-like layouts, comparisons, before/after, timelines, stacks and queues, networks, counters,
  bar charts, icons built from shapes, analogies, hook and recap scenes, anything with typography or Urdu text,
  anything that should look polished, smooth and modern.
- "manim": mathematical animation. Use it ONLY when the scene truly needs: function graphs on axes with a point moving
  along a curve, geometric constructions / proofs, vector or matrix transformations, continuous number-line math,
  equations that morph step by step.
RULE: Remotion is the default. Pick "manim" only when the scene's core idea is mathematical plotting or geometry.
When in doubt, choose "remotion".

Scenes: {json.dumps(digests[i:i + 8], ensure_ascii=False)}

Return JSON only: {{"scenes": [{{"id": 1, "engine": "remotion", "why": "short reason"}}]}}""", task="route")
            for x in as_scenes(raw):
                try:
                    out[int(x.get("id"))] = str(x.get("engine", "")).strip().lower()
                except (TypeError, ValueError):
                    pass
        return {str(i): (out.get(i) if out.get(i) in ENGINES else "remotion") for i in ids}

    saved = cached(work / "4b_route.json", ask)
    route = {i: saved.get(str(i), "remotion") for i in ids}
    n_man = sum(1 for v in route.values() if v == "manim")
    log.info("  engines: %d remotion, %d manim", len(route) - n_man, n_man)
    return route


# --------------------------------------------------------------------------- #
# Stage 5a: Remotion scene from the pinned spec
# --------------------------------------------------------------------------- #
def remotion_layout(cfg, w, h) -> str:
    if cfg.is_wide:
        return f"{cfg.aspect} wide frame, {w}x{h} px. Side-by-side layouts work well."
    return (f"{cfg.aspect} {'vertical' if h > w else 'square'} frame, {w}x{h} px. Stack things TOP TO BOTTOM (flex column); "
            "at most 2 items side by side, never 3 or more in a row. Text is BIG and lines are short (<= 4 words, split longer "
            "text into several lines). Use the full height: heading at the top, the main visual in the middle, labels below it.")


def remotion_rules(cfg, sp, w, h) -> str:
    seconds = sp["duration_sec"]
    frames = max(int(round(seconds * FPS)), FPS)
    min_px = max(int(min(w, h) * 0.04), 18)
    if cfg.screen_language == "ur":
        text = (f"- On-screen text is Urdu (right-to-left). Every element with Urdu text must use style direction: 'rtl', "
                f"textAlign: 'center' and fontFamily '\"{cfg.urdu_font}\", \"Noto Naskh Arabic\", Arial, sans-serif', and short "
                "lines (<= 6 words). Labels that are only an English term / number / formula are their own element with the "
                "normal font; never mix English and Urdu letters inside one element. No letterSpacing on Urdu text.")
    else:
        text = '- All on-screen text is short, clear English. Font: fontFamily "Inter, Segoe UI, Arial, sans-serif".'
    return f"""RULES (follow all):
- Output ONE complete TypeScript React file only, inside a single ```tsx block. No explanation.
- Imports allowed ONLY from: "react", "remotion", "@remotion/shapes", "@remotion/paths". No relative imports.
- `export default` a React component named Scene (no props). It is rendered at exactly {w}x{h} px, {FPS} fps,
  {frames} frames ({seconds:.1f} s) - this is the length of the narration, so the animation must fill it.
- SYNC (most important): every spec step has "at_sec" (when its sentence is spoken) and "max_run" (max seconds of
  its entrance animation). Build `const at = (s: number) => Math.round(s * fps);` and start the step at frame at(step.at_sec)
  (use <Sequence from={{at(..)}}> or interpolate over [at(t), at(t) + at(max_run)]). Nothing of a step may be visible before its
  at_sec. Implement EVERY step, in order. Steps with a new "phase" number replace the layout of the previous phase:
  fade the old phase out in ~0.4 s right before the new one starts; the heading (title_text) stays on screen.
  Keep the last element visible and settled from (duration - 0.8 s) to the end.
- Animate ONLY with the frame system: useCurrentFrame(), useVideoConfig(), interpolate(), spring(), Sequence,
  AbsoluteFill, Easing, random("seed"). NEVER CSS transitions/animations, setTimeout, requestAnimationFrame,
  useState/useEffect animation or Math.random.
- Layout from useVideoConfig() width/height: FRAME = {remotion_layout(cfg, w, h)} Keep 5% margin; text >= {min_px} px;
  never overflow the frame.
- Look: background {BG} (fill AbsoluteFill with it), palette {json.dumps(PAL)}; one colour per concept for the whole scene;
  rounded cards with soft shadows (boxShadow), gradient fills, subtle glow (filter: drop-shadow) on hero shapes.
- Motion that feels real: spring() entrances with a small overshoot and a direction (slide/scale in), staggered groups
  (delay each item by 4-8 frames), arrows with dots travelling along the path, counters that count up, pulses when a number
  lands, a tiny idle float (Math.sin(frame / 15) * 3) on 1-3 hero objects, SVG stroke-dashoffset drawing for lines/curves
  (strokeDasharray + interpolate), zoom by scaling a wrapper. Never leave things linear: use Easing.out/inOut(Easing.cubic).
- No images, video, audio, web fonts, remote URLs, CSS url(); draw with inline <svg>, divs, gradients, @remotion/shapes.
- No fetch / network / storage / eval / dynamic import / Node APIs.
{text}
- Short on-screen text only (labels, numbers); the narrator carries the explanation. Explain with PICTURES (analogies from
  shapes, flows, graphs that draw themselves), realise ALL listed visuals.
- Must type-check with TypeScript (React.FC, typed props / arrays)."""


def _spec_json(sp):
    spec = {k: sp[k] for k in ("id", "title_text", "duration_sec", "steps", "key_points", "visuals")}
    spec["sentences"] = [{"index": t["i"], "start": t["start"], "end": t["end"], "text": t["text"]}
                         for t in sp["timeline"]]
    return json.dumps(spec, ensure_ascii=False, indent=1)


def fallback_tsx(cfg, sp) -> str:
    """Plain, always-valid title card (heading + up to 3 key points)."""
    font = (f'"{cfg.urdu_font}", "Noto Naskh Arabic", Arial, sans-serif' if cfg.screen_language == "ur"
            else "Inter, Segoe UI, Arial, sans-serif")
    rtl = "rtl" if cfg.screen_language == "ur" else "ltr"
    title = json.dumps(str(sp.get("title_text") or sp.get("heading", "")), ensure_ascii=False)
    pts = json.dumps([str(p) for p in (sp.get("key_points") or [])[:3]], ensure_ascii=False)
    dur = float(sp["duration_sec"])
    return f'''import React from "react";
import {{AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig}} from "remotion";

const POINTS: string[] = {pts};

const Scene: React.FC = () => {{
  const frame = useCurrentFrame();
  const {{width, height, fps}} = useVideoConfig();
  const m = Math.min(width, height);
  const fade = (start: number) => interpolate(frame, [start * fps, start * fps + 24], [0, 1], {{extrapolateLeft: "clamp", extrapolateRight: "clamp"}});
  return (
    <AbsoluteFill style={{{{background: "{BG}", justifyContent: "center", alignItems: "center", direction: "{rtl}", fontFamily: {json.dumps(font)}, padding: width * 0.07}}}}>
      <div style={{{{color: "{PAL['white']}", fontSize: m * 0.075, fontWeight: 700, textAlign: "center", opacity: fade(0.1), marginBottom: m * 0.06}}}}>{{{title}}}</div>
      {{POINTS.map((p, i) => (
        <div key={{i}} style={{{{color: "{PAL['blue']}", fontSize: m * 0.05, textAlign: "center", opacity: fade(1.0 + i * Math.max(1, ({dur} - 3) / Math.max(1, POINTS.length))), marginTop: m * 0.025}}}}>{{p}}</div>
      ))}}
    </AbsoluteFill>
  );
}};
export default Scene;
'''


def build_remotion_scene(gem, cfg, sp, work: Path, env: Env, fallback=False) -> Path:
    sid = sp["id"]
    name = f"scene_{sid:02d}"
    out_dir = work / "remotion"
    out_dir.mkdir(exist_ok=True)
    final = out_dir / f"{name}.mp4"
    w, h = cfg.render_size
    seconds = float(sp["duration_sec"])
    tag = cfg.slug
    partial = out_dir / f"{name}.partial.mp4"

    def attempt(code):
        mp4, err = remotion_render(code, sid - 1, tag, cfg, FPS, seconds, env.project, env.npx, partial)
        if mp4:
            os.replace(partial, final)
            (out_dir / f"{name}.final.tsx").write_text(code + "\n", encoding="utf-8")
            return final, ""
        partial.unlink(missing_ok=True)
        return None, err

    if fallback:
        mp4, err = attempt(fallback_tsx(cfg, sp))
        if mp4:
            return mp4
        raise SceneError(f"Remotion title card for scene {sid} failed:\n{err[-800:]}")

    rules = remotion_rules(cfg, sp, w, h)
    code = extract_code(gem.code(
        "You are an expert Remotion (React + TypeScript) motion-graphics developer. Write the animation component "
        f"for this scene of a narrated explainer video. Narration audio already exists, so timing is fixed.\n"
        f"Video language of the screen text: {screen_rules(cfg)}\n\nSCENE SPEC:\n{_spec_json(sp)}\n\n{rules}", task="code"))
    err = ""
    for n in range(cfg.retries + 1):
        if n:
            log.warning("scene %s remotion repair %d/%d (%s)", sid, n, cfg.retries,
                        err.strip().splitlines()[-1][:120] if err.strip() else "")
            code = extract_code(gem.code(
                f"This Remotion TSX scene failed. Fix it and return the COMPLETE corrected file. Keep the same "
                f"visual idea and keep every step pinned to its at_sec.\n\nSCENE SPEC:\n{_spec_json(sp)}\n\n"
                f"ERROR:\n{err[-2500:]}\n\nCODE:\n```tsx\n{code}\n```\n\n{rules}",
                temperature=0.2, task="repair"))
        err = validate_tsx(code)
        if not err:
            mp4, err = attempt(code)
            if mp4:
                return mp4
    (out_dir / f"{name}.failed.tsx").write_text(code + "\n", encoding="utf-8")
    raise SceneError(f"Remotion scene {sid} failed after {cfg.retries} repair attempts:\n{err[-800:]}")


# --------------------------------------------------------------------------- #
# Stage 5: render every scene with its engine (+ cross-engine fallback)
# --------------------------------------------------------------------------- #
def stage_visuals(gem, cfg, spec, route, env: Env, work: Path):
    """-> {scene_id: mp4 path}. Resumable per scene (5_clips.json + 5_engines.json)."""
    clips_path, eng_path = work / "5_clips.json", work / "5_engines.json"
    clips, used = load_json(clips_path, {}), load_json(eng_path, {})
    for sp in spec["scenes"]:
        sid = sp["id"]
        key = str(sid)
        want = route[sid]
        rec = used.get(key)                       # no record = clip from the old Manim-only pipeline: keep it
        mine = own_scene(cfg, sp.get("custom"))   # the user's own Manim / Remotion file for this scene (None = Frame builds it)
        if (key in clips and Path(clips[key]).exists() and (rec is None or rec.get("routed") == want)
                and (not mine or str((rec or {}).get("engine", "")).startswith("own-"))):
            log.info("  scene %s cached", key)
            continue
        got, engine, errors, own_error = None, "", [], ""
        if mine:
            log.info("  scene %s: your own %s file (%s), used as it is", key, mine.get("engine"), mine.get("file_name"))
            try:
                got, engine = render_user_scene(cfg, mine, sp, work, env)
            except UserSceneError as e:
                own_error = str(e).strip().splitlines()[0][:160] if str(e).strip() else "unknown error"
                errors.append(f"own file: {str(e)[:300]}")
                log.warning("  scene %s: YOUR FILE FAILED (%s); Frame builds this scene instead\n%s", key, own_error, str(e)[-600:])
        order = [want] + [e for e in ENGINES if e != want]
        if cfg.engine_fixed:                      # "keep my engine choice fixed": never swap engines
            order = [want]
        order = [e for e in order if env.usable(e)]
        if got is not None:
            order = []
        for eng in order:
            log.info("  scene %s: %s (%s)", key, eng, "routed" if eng == want else "fallback")
            try:
                got = build_remotion_scene(gem, cfg, sp, work, env) if eng == "remotion" \
                    else manim_render(gem, cfg, sp, work)
                engine = eng
                break
            except (SceneError, RuntimeError) as e:
                errors.append(f"{eng}: {str(e)[:200]}")
                log.warning("  scene %s: %s failed (%s)", key, eng, str(e)[:160])
        if got is None and env.usable("remotion") and not cfg.engine_fixed:
            log.warning("  scene %s: using Remotion title card", key)
            got, engine = build_remotion_scene(gem, cfg, sp, work, env, fallback=True), "remotion-card"
        if got is None:
            raise RuntimeError(f"scene {sid} could not be rendered by any engine: {errors}")
        clips[key] = str(Path(got).resolve())
        used[key] = {"routed": want, "engine": engine, **({"own_error": own_error} if own_error else {})}
        save_json(clips_path, clips)
        save_json(eng_path, used)
    return {int(k): v for k, v in clips.items()}
