"""
media.py - everything that touches ffmpeg / Manim (NO Gemini calls here).

Contains: sentence timeline from real audio (silence detection), the SyncScene base
class + file header written next to every Manim scene, code pre-check, Manim render
call, fallback title-card code, per-scene audio/video sync, SRT writer and the final
assemble step (concat + .srt + manifest.json).
"""
from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
from pathlib import Path

from config import DEFAULT_ASPECT, QUALITY_FLAGS, duration, run, split_sentences, strip_fences

log = logging.getLogger("edu")


# --------------------------------------------------------------------------- #
# Sentence timeline (used right after TTS)
# --------------------------------------------------------------------------- #
def silence_intervals(path: Path, noise_db=-35, min_dur=0.15):
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
             f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(starts, ends))   # a trailing, unterminated silence is dropped on purpose


def build_timeline(narration: str, audio: Path, dur: float):
    """Start/end second of every sentence in the real audio.
    Characters give the first estimate; real pauses in the waveform correct it."""
    sents = split_sentences(narration)
    sil = silence_intervals(audio)
    speech_start = max((e for s, e in sil if s <= 0.05), default=0.0)
    speech_end = dur
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio), "-af",
             "silencedetect=noise=-35dB:d=0.15", "-f", "null", "-"])
    tail = re.findall(r"silence_start: ([\d.]+)", r.stderr)
    if tail and len(re.findall(r"silence_end", r.stderr)) < len(tail):
        speech_end = min(dur, float(tail[-1]))
    span = max(0.1, speech_end - speech_start)
    weights = [max(1, len(s)) for s in sents]
    total = float(sum(weights))
    bounds, acc = [], 0.0
    for w in weights[:-1]:
        acc += w
        bounds.append(speech_start + span * acc / total)
    mids = [(s + e) / 2 for s, e in sil if e < dur - 0.05 and s > 0.05]
    snapped, prev = [], speech_start
    for b in bounds:
        cands = [m for m in mids if abs(m - b) <= 0.7 and m > prev + 0.3]
        b2 = min(cands, key=lambda m: abs(m - b)) if cands else max(b, prev + 0.3)
        b2 = min(b2, dur - 0.1)
        snapped.append(b2)
        prev = b2
    starts = [0.0] + snapped
    ends = snapped + [dur]
    return [{"i": i, "start": round(a, 3), "end": round(b, 3), "text": t}
            for i, (a, b, t) in enumerate(zip(starts, ends, sents))]


# --------------------------------------------------------------------------- #
# Manim helpers: base class, pre-check, render, fallback
# --------------------------------------------------------------------------- #
SYNC_BASE = '''from manim import *

PAL = dict(blue="#4FC3F7", green="#81C784", orange="#FFB74D", pink="#F06292",
           yellow="#FFF176", purple="#BA68C8", white="#ECEFF1")


class SyncScene(MovingCameraScene):
    """Narration-synced scene base class (written by the pipeline, not by the model)."""

    def _now(self):
        try:
            return float(self.renderer.time)
        except Exception:
            return float(getattr(self, "time", 0.0))

    def at(self, t):
        """Wait until absolute scene time t (seconds) so the visual lands on the spoken word."""
        gap = float(t) - self._now()
        if gap > 0.04:
            self.wait(gap)

    def new_phase(self, keep=()):
        """Clear the previous layout (except `keep`) so the next phase has room."""
        gone = [m for m in self.mobjects if not any(m is k for k in keep)]
        if gone:
            self.play(FadeOut(Group(*gone)), run_time=0.5)
        self.reset_cam()

    def zoom_to(self, mob, factor=0.45, run_time=0.8):
        self.play(self.camera.frame.animate.set(width=self.camera.frame.width * factor)
                  .move_to(mob), run_time=run_time)

    def reset_cam(self):
        try:
            self.camera.frame.set(width=config.frame_width).move_to(ORIGIN)
        except Exception:
            pass

    def finish(self, total):
        """Hold the last visuals until the narration ends, then fade everything out."""
        fade = 0.8
        self.at(max(0.0, float(total) - fade))
        self.reset_cam()
        if self.mobjects:
            self.play(FadeOut(Group(*self.mobjects)), run_time=fade)
        self.at(total)
'''
FILE_HEADER = ("import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); "
               "from manim import *; from sync_base import SyncScene, PAL; "
               "config.background_color = '#0E1024'\n")


def file_header(cfg):
    """16:9 stays exactly as before. Any other shape also sets the pixel size and the Manim frame in the scene file,
    so SyncScene.reset_cam() (config.frame_width) and the model's code both see the right frame."""
    if cfg.aspect == DEFAULT_ASPECT:
        return FILE_HEADER
    (pw, ph), (fw, fh) = cfg.render_size, cfg.frame_size
    return (FILE_HEADER + f"config.pixel_width, config.pixel_height = {pw}, {ph}\n"
            f"config.frame_width, config.frame_height = {fw:.4f}, {fh:.4f}\n")


def precheck(code, cls, cfg):
    code = strip_fences(code)
    code = re.sub(r"^\s*from sync_base import.*\n", "", code, flags=re.M)
    code = re.sub(rf"class {cls}\((?:Scene|MovingCameraScene|ThreeDScene)\)", f"class {cls}(SyncScene)", code)
    if not cfg.allow_latex:
        code = re.sub(r"\b(?:MathTex|Tex)\(", "Text(", code)
    try:
        ast.parse(code)
    except SyntaxError as e:
        return code, f"SyntaxError: {e}"
    if f"class {cls}(SyncScene)" not in code:
        return code, f"Missing class {cls}(SyncScene)"
    return code, ""


def render_manim(py: Path, cls, media_dir: Path, quality, env=None, cwd=None, timeout=900):
    try:
        extra = {"env": env} if env is not None else {}
        if cwd is not None:
            extra["cwd"] = str(cwd)
        r = run(["manim", QUALITY_FLAGS[quality], "--media_dir", str(media_dir),
                 "--disable_caching", str(py), cls], timeout=timeout, **extra)
    except subprocess.TimeoutExpired:
        return None, "Render timed out (too many objects / endless loop?)"
    if r.returncode != 0:
        return None, (r.stderr or r.stdout)
    hits = list((media_dir / "videos" / py.stem).rglob(f"{cls}.mp4"))
    return (hits[0].resolve(), "") if hits else (None, "Render finished but no mp4 found")


def fallback_code(cfg, sp, cls):
    font = f", font={json.dumps(cfg.urdu_font)}" if cfg.screen_language == "ur" else ""
    title = json.dumps(sp.get("title_text") or sp.get("heading", ""), ensure_ascii=False)
    pts = json.dumps([str(p) for p in (sp.get("key_points") or [])[:3]], ensure_ascii=False)
    dur = sp["duration_sec"]
    big = not cfg.is_wide   # narrow frame: bigger text, scaled to the real frame width instead of a fixed 12 / 11
    ts, ps = (48, 34) if big else (42, 28)
    return f'''class {cls}(SyncScene):
    def construct(self):
        title = Text({title}, font_size={ts}{font}).to_edge(UP)
        if title.width > config.frame_width - 1:
            title.scale_to_fit_width(config.frame_width - 1)
        pts = VGroup(*[Text(p, font_size={ps}{font}) for p in {pts}]).arrange(DOWN, buff=0.5)
        if len(pts) > 0 and pts.width > config.frame_width - 2:
            pts.scale_to_fit_width(config.frame_width - 2)
        self.play(FadeIn(title), run_time=0.8)
        n = max(1, len(pts))
        for i, p in enumerate(pts):
            self.at(1.0 + ({dur} - 3.0) * i / n)
            self.play(FadeIn(p, shift=RIGHT * 0.3), run_time=0.8)
        self.finish({dur})
'''


def write_scene_file(py: Path, body: str, code_dir: Path, cfg):
    (code_dir / "sync_base.py").write_text(SYNC_BASE, encoding="utf-8")
    py.write_text(file_header(cfg) + body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Sync, concat, captions, manifest
# --------------------------------------------------------------------------- #
TAIL = 0.4


def video_dims(path: Path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(path)])
    try:
        w, h = r.stdout.strip().split("x")[:2]
        return int(w), int(h)
    except ValueError:
        return None


def sync_scene(video: Path, audio: Path, out: Path, size=(1280, 720)):
    """The narration always plays in full: freeze the last frame if the animation is shorter,
    pad with silence if it is longer. A clip whose shape differs from the video's (the user's own scene file) is not
    stretched: it is shown whole, over a blurred, darkened copy of itself. Returns (total, video_dur, audio_dur)."""
    vd, ad = duration(video), duration(audio)
    total = max(vd, ad) + TAIL
    extra = max(0.0, total - vd)
    W, H = size
    dims = video_dims(video)
    pad = f"tpad=stop_mode=clone:stop_duration={extra:.3f},fps=30"
    if dims and abs(dims[0] / dims[1] - W / H) > 0.02 * (W / H):
        v = (f"[0:v]{pad},split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
             f"boxblur=24:3,eq=brightness=-0.18[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
    else:
        v = f"[0:v]{pad},scale={W}:{H},format=yuv420p[v]"
    filt = f"{v};[1:a]aresample=44100,apad[a]"
    r = run(["ffmpeg", "-y", "-i", str(video), "-i", str(audio), "-filter_complex", filt,
             "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", "-c:v", "libx264",
             "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(out)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg sync failed:\n{r.stderr[-1500:]}")
    return total, vd, ad


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def stage_assemble(cfg, script, spec, audio, clips, work, final: Path):
    synced = work / "synced"
    synced.mkdir(exist_ok=True)
    lines, cues, manifest, offset = [], [], [], 0.0
    spec_by_id = {s["id"]: s for s in spec["scenes"]}
    for s in script["scenes"]:
        sid = s["id"]
        out = synced / f"scene_{sid:02d}.mp4"
        total, vd, ad = sync_scene(Path(clips[sid]), Path(audio[sid]["file"]), out, cfg.out_size)
        drift = vd - ad
        lvl = logging.WARNING if (drift < -1.0 or drift > 1.5) else logging.INFO
        log.log(lvl, "  scene %s synced: audio %.1fs, animation %.1fs (drift %+.1fs) -> %.1fs",
                sid, ad, vd, drift, total)
        tl = audio[sid]["timeline"]
        cues += [(offset + t["start"], offset + t["end"], t["text"]) for t in tl]
        sp = spec_by_id[sid]
        manifest.append({
            "scene": sid, "heading": s["heading"], "narration": s["narration"],
            "start": round(offset, 2), "end": round(offset + total, 2),
            "audio_file": audio[sid]["file"], "video_clip": str(clips[sid]),
            "audio_sec": round(ad, 2), "animation_sec": round(vd, 2), "drift_sec": round(drift, 2),
            "sentences": tl, "title_text": sp["title_text"], "key_points": sp["key_points"],
            "visuals": sp["visuals"], "steps": sp["steps"], "plan": sp.get("custom")})
        offset += total
        lines.append(f"file '{out.resolve().as_posix()}'")
    lst = work / "concat.txt"
    lst.write_text("\n".join(lines), encoding="utf-8")
    final.parent.mkdir(parents=True, exist_ok=True)
    r = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(final)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed:\n{r.stderr[-1500:]}")
    srt = "\n".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{txt}\n"
                    for i, (a, b, txt) in enumerate(cues, 1))
    final.with_suffix(".srt").write_text(srt, encoding="utf-8")
    final.with_suffix(".manifest.json").write_text(
        json.dumps({"title": script["title"], "language": cfg.language,
                    "voice": cfg.voice, "aspect": cfg.aspect, "total_sec": round(offset, 2), "scenes": manifest},
                   indent=2, ensure_ascii=False), encoding="utf-8")
