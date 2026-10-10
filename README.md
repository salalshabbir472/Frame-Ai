# Educational Video Pipeline (Gemini free tier, English / Urdu)

Topic dalo -> script -> Gemini TTS audio -> concepts/visuals -> Manim spec ->
Gemini-written Manim code -> render -> sync -> final **MP4 + .srt + manifest.json**

Pehle ek hi `pipeline.py` thi (~1200 lines). Ab 6 chhoti files ma divide hai. **Logic bilkul same hai**,
sirf code alag files ma rakha gaya hai.

---

## Files ka overview

| File | Gemini call? | Kya hai isme |
|------|:---:|---|
| `config.py` | ❌ | Settings + chhote helpers |
| `gemini_client.py` | ✅ (text/code API) | Gemini ka poora handling: key rotation, errors, models |
| `tts.py` | ✅ (TTS API) | Awaaz banana (Gemini TTS + Google Cloud TTS reserve) |
| `stages_gemini.py` | ✅ | Wo saare pipeline stages jahan Gemini call hoti hai |
| `media.py` | ❌ | ffmpeg / Manim wala kaam (timeline, render, sync, assemble) |
| `pipeline.py` | ❌ (keys yahan load hoti hain) | Entry point: `make_video()` + command line (CLI) |

---

## 1. `config.py` — settings aur helpers
Koi network / Gemini call nahi.

- `QUALITY_FLAGS` — Manim quality (low / medium / high)
- `DEFAULT_LIGHT_MODELS`, `DEFAULT_CODE_MODELS`, `DEFAULT_TTS_MODELS` — Gemini model fallback chains
- `LANGS` — language settings (`en`, `ur`: words-per-second, RTL, cloud voices)
- `VideoConfig` — **single control surface**: topic, minutes, language, voice, models, quality, retries, urdu_font...
- Helpers: `load_env` (.env padhna), `env_list` (comma-separated keys), `csv`, `run` (subprocess),
  `duration` (ffprobe), `slugify`, `strip_fences`, `as_scenes`, `cached` (disk cache),
  `secs_to_pacific_midnight` (daily quota reset time), `split_sentences`

## 2. `gemini_client.py` — Gemini handling (text + code)
Yahan sab kuch hai jo Gemini API se baat karne ke liye chahiye.

- `BadOutput`, `QuotaExhausted` — custom errors
- `Rotator` — key / model rotation engine: 429 par agli key, cooldown, dead key/model drop
- `http_options()` — SDK ke retry settings
- `classify_gemini(e)` — error ko samajhna (rate limit / bad key / bad model / transient)
- `Gemini` class:
  - `.json(prompt)` — sasta JSON kaam (script, analysis, spec) -> light models
  - `.code(prompt)` — Manim code likhna -> code models
- `list_models()` — `--list-models` flag ke liye

## 3. `tts.py` — awaaz (Text-to-Speech)
Gemini TTS pehle, Google Cloud TTS optional reserve.

- `classify_cloud(e)` — Cloud TTS errors
- `pcm_to_wav`, `as_wav` — audio ko WAV banana
- `TTS` class — `speak(text, cfg, out_wav)`: text ko chunks ma todna -> Gemini TTS -> (fail ho to Cloud TTS) -> ffmpeg se
  ek 24kHz mono WAV
- `list_cloud_voices()` — `--list-cloud-voices` flag ke liye

## 4. `stages_gemini.py` — Gemini wale pipeline stages
Jahan jahan Gemini (text / TTS / code) call hoti hai wo saare stages.

- `narration_rules`, `screen_rules` — language ke hisaab se prompt blocks
- **Stage 1** `stage_script` — topic se script (scenes + narration)
- **Stage 2** `stage_audio` — har scene ki narration TTS se audio + sentence timeline
- **Stage 3** `stage_analyze` — concepts, examples, key points, visuals
- **Stage 4** `pin_steps`, `stage_spec` — Manim spec, har step ek bole gaye sentence se pinned
- **Stage 5** `manim_rules`, `render_scene`, `stage_manim` — Gemini se Manim code likhwana, render karna,
  fail ho to auto-repair, aakhir ma fallback title card

## 5. `media.py` — ffmpeg / Manim (bina Gemini)
- `silence_intervals`, `build_timeline` — real audio se har sentence ka start/end time
- `SYNC_BASE`, `FILE_HEADER` — `SyncScene` base class jo har Manim file ke saath likhi jati hai
- `precheck` — generated code ki syntax / class check
- `render_manim` — `manim` command chalana
- `fallback_code` — Gemini fail ho jaye to simple title-card code
- `write_scene_file` — scene `.py` file likhna
- `sync_scene` — audio aur video ko align karna (last frame freeze / silence pad)
- `srt_time`, `stage_assemble` — final concat + `.srt` + `manifest.json`

## 6. `pipeline.py` — entry point
- `Services`, `get_services` — **Gemini keys yahan env/.env se load hoti hain**, `Gemini` aur `TTS` objects banate hain
- `check_urdu_font` — Urdu font installed hai ya nahi
- `make_video(cfg)` — **main function**: 6 stages ek ke baad ek chalata hai
- `tts_test`, `main` — command line interface

`pipeline.py` abhi bhi `VideoConfig` aur `make_video` export karti hai, to purana
`from pipeline import VideoConfig, make_video` kaam karta rahega.

---

## Import dependencies (circular import nahi hai)

```
config.py            <- (koi nahi)
gemini_client.py     <- config
tts.py               <- config, gemini_client
media.py             <- config
stages_gemini.py     <- config, gemini_client, media
pipeline.py          <- sab
```

## Data flow (kaun si file kab chalti hai)

```
pipeline.make_video
  1 stage_script   (stages_gemini -> gemini_client)
  2 stage_audio    (stages_gemini -> tts, media.build_timeline)
  3 stage_analyze  (stages_gemini -> gemini_client)
  4 stage_spec     (stages_gemini -> gemini_client)
  5 stage_manim    (stages_gemini -> gemini_client + media.render_manim)
  6 stage_assemble (media)
```

## Setup

```bash
pip install google-genai manim
# optional (Cloud TTS reserve): pip install google-cloud-texttospeech
# ffmpeg + ffprobe PATH ma hone chahiye
# Urdu on-screen text ke liye: sudo apt install fonts-noto-core
```

`.env` file:
```
GEMINI_API_KEYS=key1,key2,key3
```

## Chalane ka tareeqa

```bash
python pipeline.py "How does gradient descent work?" --minutes 3 --language en
python pipeline.py "گریڈینٹ ڈیسنٹ کیا ہے؟" --language ur --voice Kore
python pipeline.py --config job.json
python pipeline.py --topics-file topics.txt
python pipeline.py --tts-test --language ur
python pipeline.py --list-models
```

Python se:
```python
from pipeline import VideoConfig, make_video
make_video(VideoConfig(topic="How does gradient descent work?", minutes=3,
                       language="ur", screen_language="en", voice="Kore"))
```

## Output / cache
- `build/<slug>/` — har stage ka cache (`1_script.json`, `2_audio.json`, `3_analysis.json`, `4_manim_spec.json`,
  `5_clips.json`), `pipeline.log`. Crash ya quota khatam ho jaye to dobara chalao, wahin se resume hoga.
- `output/<slug>.mp4`, `.srt`, `.manifest.json` — final files.

## Naya kaam kahan karein?
- Naya Gemini prompt / stage -> `stages_gemini.py`
- Key / model / quota handling -> `gemini_client.py`
- Awaaz / TTS engine -> `tts.py`
- Video sync, subtitles, ffmpeg -> `media.py`
- Naya setting (jaise `--fps`) -> `config.py` (`VideoConfig`)
