"""
Automated educational-video pipeline (Gemini free tier, English / Urdu)

    topic -> script -> Gemini TTS (real audio durations + per-sentence timeline)
          -> concepts / key points / visuals -> Manim spec (steps pinned to sentences)
          -> Gemini-written Manim code -> render (auto-repair) -> per-scene sync
          -> final MP4 + .srt + manifest.json

ONE entry point controls everything:

    from pipeline import VideoConfig, make_video
    make_video(VideoConfig(topic="How does gradient descent work?", minutes=3,
                           language="ur", screen_language="en", voice="Kore"))

CLI:
    python pipeline.py "How does gradient descent work?" --minutes 3 --language en
    python pipeline.py "گریڈینٹ ڈیسنٹ کیا ہے؟" --language ur --voice Kore
    python pipeline.py --config job.json            # one job or a list of jobs
    python pipeline.py --topics-file topics.txt     # unattended batch
    python pipeline.py --tts-test --language ur     # hear the voice before a full run

Free-tier hardening
  * Many Gemini keys: on 429 / quota / invalid key the next key is used; cooled-down
    keys come back by themselves. NOTE: Google rate limits are per *project*, not per
    key, so extra keys only help if they come from different Google Cloud projects.
  * Model fallback chains for text, for code and for TTS (a model the project cannot
    use is dropped automatically, never the key).
  * Every stage is cached on disk, so a crash or a daily-quota wall just resumes later.

Module layout: see README.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from dataclasses import fields
from pathlib import Path

from config import VideoConfig, cached, csv, duration, env_list, load_env, run
from gemini_client import Gemini, QuotaExhausted, list_models
from media import stage_assemble
from script_input import stage_script_text
from stages_gemini import (stage_analyze, stage_audio, stage_manim, stage_script,
                           stage_spec)
from tts import TTS, list_cloud_voices

log = logging.getLogger("edu")


# --------------------------------------------------------------------------- #
# Services: builds the Gemini + TTS clients from the API keys in env / .env
# --------------------------------------------------------------------------- #
class Services:
    def __init__(self, gem: Gemini, tts: TTS):
        self.gem, self.tts = gem, tts


_SERVICES = {}


def get_services(cfg: VideoConfig) -> Services:
    sig = (cfg.light_models, cfg.medium_models, cfg.code_models, cfg.tts_models, cfg.rpm, cfg.tts_rpm, cfg.max_wait)
    if sig in _SERVICES:
        return _SERVICES[sig]
    keys = env_list("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    if not keys:
        raise SystemExit("No Gemini keys. Set GEMINI_API_KEYS=key1,key2,... (env or .env)")
    cloud = env_list("TTS_API_KEYS", "TTS_CREDENTIAL_FILES")
    if not cloud and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        cloud = ["default"]
    gem = Gemini(keys, csv(cfg.light_models), csv(cfg.code_models), cfg.rpm, cfg.max_wait,
                 medium_models=csv(cfg.medium_models))
    tts = TTS(keys, cfg.tts_rpm, cfg.max_wait, cloud)
    log.info("%d Gemini key(s); text %s; code %s; tts %s; cloud TTS reserve: %s",
             len(keys), gem.chain("analyze")[:3], gem.chain("code")[:3], csv(cfg.tts_models),
             f"{len(cloud)} credential(s)" if tts.c else "off")
    _SERVICES[sig] = Services(gem, tts)
    return _SERVICES[sig]


# --------------------------------------------------------------------------- #
# THE function: everything is controlled through VideoConfig
# --------------------------------------------------------------------------- #
def check_urdu_font(cfg):
    if cfg.screen_language != "ur" or not shutil.which("fc-list"):
        return
    fams = run(["fc-list", ":", "family"]).stdout.lower()
    if cfg.urdu_font.lower() not in fams:
        log.warning("Font %r not installed: Urdu on-screen text will render as boxes. "
                    "Install one (e.g. `sudo apt install fonts-noto-core`) and check "
                    "`fc-list :lang=ur family`, or set urdu_font / use screen_language='en'.",
                    cfg.urdu_font)


def make_video(cfg: VideoConfig, services: Services | None = None) -> Path:
    svc = services or get_services(cfg)
    gem, tts = svc.gem, svc.tts
    work = Path(cfg.workdir) / cfg.slug
    work.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(work / "pipeline.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    try:
        log.info("=== %s  [%s, narration=%s, screen=%s, voice=%s] ===",
                 cfg.topic, "own script" if cfg.input_type == "script" else "from prompt",
                 cfg.language, cfg.screen_language, cfg.voice)
        check_urdu_font(cfg)
        log.info("[1/6] Script")
        write = stage_script_text if cfg.input_type == "script" else stage_script   # own script: split it, never rewrite it
        script = cached(work / "1_script.json", lambda: write(gem, cfg, work))
        log.info("[2/6] Narration audio + sentence timeline")
        audio = stage_audio(tts, cfg, script, work)
        log.info("[3/6] Concepts, examples, key points, visuals")
        analysis = cached(work / "3_analysis.json", lambda: stage_analyze(gem, cfg, script))
        log.info("[4/6] Manim visual spec (steps pinned to spoken sentences)")
        spec = cached(work / "4_manim_spec.json", lambda: stage_spec(gem, cfg, script, audio, analysis))
        log.info("[5/6] Manim code + render")
        clips = stage_manim(gem, cfg, spec, work)
        log.info("[6/6] Sync + assemble")
        final = Path(cfg.output_dir) / f"{cfg.slug}.mp4"
        stage_assemble(cfg, script, spec, audio, clips, work, final)
        log.info("DONE: %s (+ .srt, .manifest.json)", final)
        return final
    finally:
        log.removeHandler(fh)
        fh.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def tts_test(cfg: VideoConfig):
    sample = {"en": "This is a short test of the narration voice. Does it sound right to you?",
              "ur": "یہ آواز کا ایک مختصر تجربہ ہے۔ کیا یہ آپ کو ٹھیک لگ رہی ہے؟"}[cfg.language]
    out = Path(cfg.output_dir) / f"tts_test_{cfg.language}_{cfg.voice}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    get_services(cfg).tts.speak(sample, cfg, out)
    print(f"Wrote {out} ({duration(out):.1f}s). Listen to it before a full run.")


def main():
    load_env()
    ap = argparse.ArgumentParser(description="Topic -> educational video (Gemini free tier, en/ur)")
    ap.add_argument("topic", nargs="?")
    ap.add_argument("--topics-file", help="one topic per line (# comments allowed)")
    ap.add_argument("--config", help="JSON file: one job object or a list of jobs "
                                     "(any VideoConfig field)")
    for f in fields(VideoConfig):
        if f.name == "topic":
            continue
        flag = "--" + f.name.replace("_", "-")
        if f.type in ("bool", bool):
            ap.add_argument(flag, action="store_true", default=None)
        else:
            ap.add_argument(flag, default=None,
                            type=float if f.type in ("float", float) else
                            int if f.type in ("int", int) else str)
    ap.add_argument("--script-file", help="text file with a finished script; the topic argument is then the video title")
    ap.add_argument("--list-models", action="store_true",
                    help="print the Flash / TTS models your first Gemini key can see, then exit")
    ap.add_argument("--list-cloud-voices", metavar="LANG_CODE",
                    help="print Cloud TTS voices for e.g. ur-IN / en-US, then exit")
    ap.add_argument("--tts-test", action="store_true", help="synthesize one sample sentence, then exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    names = {f.name for f in fields(VideoConfig)}
    base = {k: v for k, v in vars(args).items() if k in names and v is not None and k != "topic"}

    if args.script_file:
        base["script"] = Path(args.script_file).read_text(encoding="utf-8")
    if args.list_models:
        list_models()
        return
    if args.list_cloud_voices:
        list_cloud_voices(args.list_cloud_voices)
        return

    jobs = []
    if args.topic:
        jobs.append({"topic": args.topic})
    if args.topics_file:
        jobs += [{"topic": l.strip()} for l in Path(args.topics_file).read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
    if args.config:
        data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        jobs += data if isinstance(data, list) else [data]
    if args.tts_test:
        jobs = jobs or [{"topic": "tts-test"}]
    if not jobs:
        sys.exit("Give a topic, --topics-file or --config")

    for tool in ("manim", "ffmpeg", "ffprobe"):
        if not args.tts_test and not shutil.which(tool):
            sys.exit(f"'{tool}' not found on PATH (see README)")
    if not shutil.which("ffmpeg"):
        sys.exit("'ffmpeg' not found on PATH")

    try:
        cfgs = []
        for j in jobs:
            bad = set(j) - names
            if bad:
                sys.exit(f"Unknown config field(s) {sorted(bad)}. Valid: {sorted(names)}")
            cfgs.append(VideoConfig(**{**base, **j}))
    except ValueError as e:
        sys.exit(f"Config error: {e}")

    if args.tts_test:
        tts_test(cfgs[0])
        return

    results = {}
    for cfg in cfgs:
        try:
            results[cfg.topic] = str(make_video(cfg))
        except QuotaExhausted as e:
            log.error("%s", e)
            results[cfg.topic] = f"QUOTA: {e}"
            break   # no point trying more topics until the quota resets
        except Exception as e:  # noqa: BLE001
            log.exception("topic failed: %s", cfg.topic)
            results[cfg.topic] = f"FAILED: {e}"

    print("\nSummary")
    for t, r in results.items():
        print(f"  {t[:60]:60s} {r.splitlines()[0]}")
    sys.exit(0 if all(not r.startswith(("FAILED", "QUOTA")) for r in results.values()) else 1)


if __name__ == "__main__":
    main()
