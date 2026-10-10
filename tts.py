"""
tts.py - text-to-speech engines (Gemini TTS first, Google Cloud TTS as optional reserve).

Contains: Cloud-TTS error classification, PCM->WAV helpers, the `TTS` class
(`TTS.speak(text, cfg, out_wav)` chunks the text, tries Gemini TTS models/keys, falls
back to Cloud TTS, then joins everything into one normalised 24 kHz mono WAV) and
`list_cloud_voices()` for the --list-cloud-voices CLI flag.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import sys
from pathlib import Path

from google import genai
from google.genai import types

try:  # optional: Google Cloud TTS is only a reserve engine (Urdu quality / quota)
    from google.api_core import exceptions as gex
    from google.auth import exceptions as auth_exc
    from google.cloud import texttospeech
    HAVE_CLOUD_TTS = True
except Exception:  # noqa: BLE001
    HAVE_CLOUD_TTS = False

from config import LANGS, csv, duration, env_list, run, split_sentences
from gemini_client import BadOutput, Rotator, classify_gemini, http_options, rpm_for

log = logging.getLogger("edu")

# Per-scene TTS cache shared by ALL runs (survives deleting the work folder).
TTS_CACHE = Path(os.environ.get("TTS_CACHE_DIR", "tts_cache"))


def cache_key(cfg, text):
    raw = "|".join([str(cfg.tts_engine), str(cfg.tts_models), str(cfg.voice), str(cfg.tts_style),
                    str(getattr(cfg, "cloud_voices", "")), text])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classify_cloud(e):
    low = str(e).lower()
    if isinstance(e, gex.TooManyRequests):
        return "rate", (6 * 3600 if "month" in low else 65.0)
    if isinstance(e, (gex.PermissionDenied, gex.Unauthenticated, auth_exc.GoogleAuthError,
                      FileNotFoundError)):
        return "bad_key", 0
    if isinstance(e, gex.InvalidArgument):
        if "api key" in low:
            return "bad_key", 0
        if "voice" in low or "language" in low:
            return "bad_variant", 0
        return "fatal", 0
    if isinstance(e, gex.NotFound):
        return "bad_variant", 0
    if isinstance(e, (gex.ServiceUnavailable, gex.DeadlineExceeded, gex.InternalServerError,
                      gex.GatewayTimeout, ConnectionError, TimeoutError)):
        return "transient", 5
    return "fatal", 0


def pcm_to_wav(pcm: bytes, rate=24000) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def as_wav(audio: bytes) -> bytes:
    """Gemini 3.8 returns WAV, 3.1 returns headerless 24 kHz 16-bit PCM."""
    return audio if audio[:4] == b"RIFF" else pcm_to_wav(audio)


class TTS:
    def __init__(self, gem_keys, rpm, max_wait, cloud_entries):
        self.g = Rotator("tts-gemini", gem_keys, classify_gemini,
                         min_interval=60.0 / max(rpm, 0.5), max_wait=max_wait, max_attempts=6,
                         interval_for=lambda m: 60.0 / max(rpm_for(m, rpm), 0.5))
        self.c = (Rotator("tts-cloud", cloud_entries, classify_cloud, max_wait=max_wait)
                  if cloud_entries and HAVE_CLOUD_TTS else None)
        self._gclients, self._cclients = {}, {}

    # -- engines
    def _gclient(self, key):
        if key not in self._gclients:
            self._gclients[key] = genai.Client(
                api_key=key, http_options=http_options())
        return self._gclients[key]

    def _gemini(self, key, model, text, voice, style):
        client = self._gclient(key)
        if model.startswith("gemini-3.8"):
            # 3.8 treats `text` as a verbatim transcript; delivery goes in speech_metadata
            block = {"type": "text", "text": text}
            if style:
                block["annotations"] = [{"type": "speech_metadata", "style": style}]
            it = client.interactions.create(
                model=model,
                input=[{"type": "user_input", "content": [block]}],
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": voice}]})
            oa = getattr(it, "output_audio", None)
            data = getattr(oa, "data", None) if oa else None
            if not data:
                raise BadOutput("no audio in interaction")
            return as_wav(base64.b64decode(data) if isinstance(data, str) else data)
        prompt = f"Say in a {style} voice: {text}" if style else text   # 3.1 / older style prompt
        r = client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
        try:
            data = r.candidates[0].content.parts[0].inline_data.data
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            raise BadOutput("no audio in response (finishReason OTHER?)")
        return as_wav(data)

    def _cclient(self, entry):
        if entry not in self._cclients:
            if entry == "default":
                c = texttospeech.TextToSpeechClient()
            elif entry.lower().endswith(".json"):
                c = texttospeech.TextToSpeechClient.from_service_account_file(entry)
            else:
                c = texttospeech.TextToSpeechClient(client_options={"api_key": entry})
            self._cclients[entry] = c
        return self._cclients[entry]

    def _cloud(self, entry, voice, text):
        lang = "-".join(voice.split("-")[:2])
        r = self._cclient(entry).synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(language_code=lang, name=voice),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=24000),
            timeout=60)
        return as_wav(r.audio_content)

    # -- one chunk, with engine fallback
    def _chunk(self, text, cfg, gemini_models, cloud_voices, allow_cloud=True):
        err = None
        if cfg.tts_engine in ("auto", "gemini"):
            try:
                return self.g.run(lambda k, m: self._gemini(k, m, text, cfg.voice, cfg.tts_style),
                                  gemini_models, label="speech")
            except Exception as e:  # noqa: BLE001
                err = e
                if cfg.tts_engine == "gemini" or self.c is None or not allow_cloud:
                    raise
                log.warning("Gemini TTS unavailable (%s); switching to Cloud TTS", str(e)[:120])
        if self.c is None:
            raise RuntimeError("tts_engine='cloud' but no Cloud TTS credentials "
                               "(TTS_API_KEYS / TTS_CREDENTIAL_FILES / GOOGLE_APPLICATION_CREDENTIALS)")
        return self.c.run(lambda k, v: self._cloud(k, v, text), cloud_voices)

    @staticmethod
    def _chunks(text, limit=1800):
        chunks, cur = [], ""
        for s in split_sentences(text):
            if cur and len(cur) + 1 + len(s) > limit:
                chunks.append(cur)
                cur = s
            else:
                cur = (cur + " " + s).strip()
        if cur:
            chunks.append(cur)
        return chunks

    def speak(self, text, cfg, out_wav: Path, limit=1800, allow_cloud=True):
        """Synthesize `text` to a normalised 24 kHz mono WAV at out_wav."""
        gemini_models = csv(cfg.tts_models)
        cloud_voices = csv(cfg.cloud_voices or LANGS[cfg.language]["cloud_voices"])
        parts = []
        for i, chunk in enumerate(self._chunks(text, limit)):
            p = out_wav.with_name(f"{out_wav.stem}.part{i}.wav")
            p.write_bytes(self._chunk(chunk, cfg, gemini_models, cloud_voices, allow_cloud))
            parts.append(p)
        lst = out_wav.with_suffix(".txt")
        lst.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in parts), encoding="utf-8")
        r = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                 "-ar", "24000", "-ac", "1", str(out_wav)])
        for p in parts:
            p.unlink(missing_ok=True)
        lst.unlink(missing_ok=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg audio normalise failed:\n{r.stderr[-800:]}")

    # -- batching + per-scene cache -------------------------------------------------
    @staticmethod
    def _groups(items, max_scenes, max_chars):
        out, cur, n = [], [], 0
        for it in items:
            L = len(it[1])
            if cur and (len(cur) >= max_scenes or n + L > max_chars):
                out.append(cur)
                cur, n = [], 0
            cur.append(it)
            n += L
        if cur:
            out.append(cur)
        return out

    def _finish(self, sid, wav, cached, on_done):
        shutil.copy(wav, cached)
        if on_done:
            on_done(sid, wav)

    def _speak_group(self, group, cfg, on_done):
        """One TTS call for several scenes, then slice at the scene boundaries.
        All-or-nothing: returns False (nothing written) if the boundaries are not trustworthy."""
        from media import build_timeline, silence_intervals   # lazy: media does not import tts
        texts = [g[1] for g in group]
        joined = " ".join(texts)
        tmp = group[0][2].with_name(f"group_{group[0][0]}_{group[-1][0]}.wav")
        outs = [g[2] for g in group]
        try:
            self.speak(joined, cfg, tmp, limit=len(joined) + 1, allow_cloud=False)
            d = duration(tmp)
            counts = [len(split_sentences(t)) for t in texts]
            tl = build_timeline(joined, tmp, d)
            if len(tl) != sum(counts):
                raise ValueError(f"sentence count mismatch ({len(tl)} vs {sum(counts)})")
            bounds, idx = [], 0
            for c in counts[:-1]:
                idx += c
                bounds.append(tl[idx]["start"])
            sil = silence_intervals(tmp)
            for b in bounds:       # every cut must sit inside a real pause
                if not any(s - 0.05 <= b <= e + 0.05 for s, e in sil):
                    raise ValueError(f"no clear pause at {b:.2f}s")
            edges = [0.0] + bounds + [d]
            if any(b - a < 0.8 for a, b in zip(edges, edges[1:])):
                raise ValueError("scene slice too short")
            for w, a, b in zip(outs, edges, edges[1:]):
                r = run(["ffmpeg", "-y", "-i", str(tmp), "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                         "-ar", "24000", "-ac", "1", str(w)])
                if r.returncode != 0:
                    raise RuntimeError(r.stderr[-300:])
        except Exception as e:  # noqa: BLE001
            log.warning("  group %s-%s failed (%s); falling back to scene-by-scene",
                        group[0][0], group[-1][0], str(e)[:150])
            for w in outs:
                w.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            return False
        tmp.unlink(missing_ok=True)
        for sid, _, w, cached in group:
            self._finish(sid, w, cached, on_done)
        return True

    def speak_many(self, scenes, cfg, on_done=None, group_scenes=None, group_chars=None):
        """scenes: [(sid, text, out_wav)]. Cache hit -> 0 calls; misses are sent 3-4 scenes per
        call and sliced back apart. on_done(sid, wav) fires as soon as each scene's wav is ready."""
        group_scenes = int(group_scenes or getattr(cfg, "tts_group_scenes", 4))
        group_chars = int(group_chars or getattr(cfg, "tts_group_chars", 4000))
        if cfg.tts_engine == "cloud":      # Cloud TTS has a small per-request limit: no batching
            group_scenes = 1
        TTS_CACHE.mkdir(parents=True, exist_ok=True)
        misses = []
        for sid, text, wav in scenes:
            cached = TTS_CACHE / f"{cache_key(cfg, text)}.wav"
            if cached.exists() and cached.stat().st_size > 0:
                shutil.copy(cached, wav)
                log.info("  scene %s: TTS cache hit (0 API calls)", sid)
                if on_done:
                    on_done(sid, wav)
            else:
                misses.append((sid, text, wav, cached))
        for group in self._groups(misses, group_scenes, group_chars):
            log.info("  TTS call for scene(s) %s (%d chars)", [g[0] for g in group],
                     sum(len(g[1]) for g in group))
            if len(group) > 1 and self._speak_group(group, cfg, on_done):
                continue
            for sid, text, wav, cached in group:
                self.speak(text, cfg, wav)
                self._finish(sid, wav, cached, on_done)


def list_cloud_voices(lang_code):
    """Print Cloud TTS voices for e.g. ur-IN / en-US (--list-cloud-voices)."""
    if not HAVE_CLOUD_TTS:
        sys.exit("pip install google-cloud-texttospeech first")
    entry = (env_list("TTS_API_KEYS", "TTS_CREDENTIAL_FILES") or ["default"])[0]
    t = TTS([], 4, 1, [entry])
    for v in t._cclient(entry).list_voices(language_code=lang_code).voices:
        print(v.name)
