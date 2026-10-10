"""
sfx.py - procedural sound effects for the Manim scenes (no audio files needed).

Every spec step already has an `action` and a pinned `at_sec`, so the sound track is built
straight from the spec: pop on create, whoosh on move/flow, chime on highlight, ticks on
count_up, pen-scratch on write ... Then it is mixed UNDER the narration (with ducking, so the
voice always stays clear) plus an optional very quiet ambient pad.

Public API
    build_scene_mix(narration_wav, steps, out_wav, level=0.22, ambient=True, seed=0)
        -> writes out_wav = narration + sfx (same sample rate / length as narration)
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# wav io (16-bit PCM, any channel count -> mono float in, mono wav out)
# --------------------------------------------------------------------------- #
def read_wav(path):
    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"{path}: only 16-bit PCM wav supported (got {8 * sw}-bit)")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def write_wav(path, x, sr):
    x = np.clip(x, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())


# --------------------------------------------------------------------------- #
# building blocks
# --------------------------------------------------------------------------- #
def _t(sr, dur):
    return np.arange(int(sr * dur), dtype=np.float32) / sr


def _lowpass(x, cutoff, sr):
    """One-pole low-pass; `cutoff` may be a scalar or a per-sample array (Hz)."""
    cutoff = np.broadcast_to(np.asarray(cutoff, dtype=np.float32), x.shape)
    a = 1.0 - np.exp(-2.0 * np.pi * cutoff / sr)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc += a[i] * (x[i] - acc)
        y[i] = acc
    return y


def _fade_edges(x, sr, ms=4):
    k = max(1, int(sr * ms / 1000))
    if len(x) > 2 * k:
        x[:k] *= np.linspace(0, 1, k)
        x[-k:] *= np.linspace(1, 0, k)
    return x


def _rng(seed):
    return np.random.default_rng(seed)


def whoosh(sr, dur=0.5, up=True, bright=1.0, seed=0):
    """Air sweep: noise through a low-pass whose cutoff glides up (or down) with a bell envelope."""
    t = _t(sr, dur)
    n = _rng(seed).standard_normal(len(t)).astype(np.float32)
    g = np.linspace(0, 1, len(t))
    g = g if up else g[::-1]
    fc = (300 + 3800 * bright * g ** 1.5).astype(np.float32)
    y = _lowpass(n, fc, sr) - _lowpass(n, 120, sr)
    env = np.sin(np.pi * np.linspace(0, 1, len(t))) ** 1.6
    y *= env
    return _fade_edges(y / (np.max(np.abs(y)) + 1e-9) * 0.9, sr)


def pop(sr, f0=520.0, dur=0.16, rise=False):
    """Soft bubble pop: sine with fast pitch drop (or rise) + short body."""
    t = _t(sr, dur)
    sweep = (1 - np.exp(-t * 45)) if rise else np.exp(-t * 45)
    f = f0 * (1 + (0.9 if not rise else 0.7) * sweep)
    ph = 2 * np.pi * np.cumsum(f) / sr
    y = np.sin(ph) * np.exp(-t * 26)
    y += 0.25 * np.sin(2 * ph) * np.exp(-t * 40)
    return _fade_edges(y.astype(np.float32) * 0.9, sr, 2)


def tick(sr, f=1900.0, seed=0):
    t = _t(sr, 0.045)
    n = _rng(seed).standard_normal(len(t)).astype(np.float32)
    y = np.sin(2 * np.pi * f * t) * np.exp(-t * 110) + 0.35 * (n - _lowpass(n, 2500, sr)) * np.exp(-t * 160)
    return _fade_edges(y.astype(np.float32) * 0.7, sr, 1)


def chime(sr, f=880.0, dur=0.7, bright=1.0):
    """Glassy bell: inharmonic partials with different decay rates."""
    t = _t(sr, dur)
    y = np.zeros_like(t)
    for ratio, amp, dec in ((1, 1.0, 5.0), (2.01, 0.45 * bright, 8.0), (2.76, 0.30 * bright, 11.0),
                            (5.4, 0.12 * bright, 18.0)):
        y += amp * np.sin(2 * np.pi * f * ratio * t) * np.exp(-t * dec)
    y *= np.minimum(1, t / 0.004)
    return _fade_edges(y.astype(np.float32) / (np.max(np.abs(y)) + 1e-9) * 0.8, sr, 3)


def scratch(sr, dur=0.45, seed=0):
    """Marker/pen writing: high-passed noise chopped by a fast random-ish envelope."""
    t = _t(sr, dur)
    r = _rng(seed)
    n = r.standard_normal(len(t)).astype(np.float32)
    n = n - _lowpass(n, 1800, sr)
    am = 0.55 + 0.45 * np.abs(np.sin(2 * np.pi * (14 + 6 * r.random()) * t + r.random() * 6))
    env = np.minimum(1, t / 0.03) * np.minimum(1, (dur - t) / 0.08)
    y = n * am * env
    return _fade_edges(y / (np.max(np.abs(y)) + 1e-9) * 0.55, sr)


def sweep(sr, dur=0.38, f0=280.0, f1=900.0):
    """Rising 'morph' tone for transform."""
    t = _t(sr, dur)
    f = f0 * (f1 / f0) ** (t / dur)
    ph = 2 * np.pi * np.cumsum(f) / sr
    env = np.sin(np.pi * t / dur) ** 2
    y = (np.sin(ph) + 0.3 * np.sin(2 * ph)) * env
    return _fade_edges(y.astype(np.float32) * 0.55, sr)


def thump(sr, dur=0.55, f0=95.0):
    """Low zoom / phase-change boom."""
    t = _t(sr, dur)
    f = f0 * (1 + 1.2 * np.exp(-t * 14))
    ph = 2 * np.pi * np.cumsum(f) / sr
    y = np.sin(ph) * np.exp(-t * 7) * np.minimum(1, t / 0.006)
    return _fade_edges(y.astype(np.float32) * 0.9, sr)


def ambient_pad(sr, dur, seed=0):
    """Very quiet warm pad (A minor add9-ish) so the video never feels 'dead air'."""
    t = _t(sr, dur)
    y = np.zeros_like(t)
    for f, a in ((110.0, 1.0), (164.81, 0.7), (220.0, 0.8), (261.63, 0.5), (329.63, 0.35)):
        lfo = 0.6 + 0.4 * np.sin(2 * np.pi * (0.07 + 0.05 * _rng(int(f)).random()) * t + f)
        y += a * lfo * np.sin(2 * np.pi * f * t + _rng(seed + int(f)).random() * 6)
    fade = np.minimum(1, np.minimum(t / 1.2, (dur - t) / 1.5))
    return (y * fade / (np.max(np.abs(y)) + 1e-9)).astype(np.float32)


# --------------------------------------------------------------------------- #
# step -> sound events
# --------------------------------------------------------------------------- #
def _events(steps, dur, sr, seed):
    """[(time_sec, samples, gain)] built from the pinned spec steps."""
    ev = [(0.05, pop(sr, 420, 0.22), 0.7)]                       # heading appears
    last_phase, last_t = None, -1.0
    for k, st in enumerate(sorted(steps, key=lambda s: s.get("at_sec", 0))):
        at = float(st.get("at_sec", 0.0))
        run = float(st.get("max_run", 0.6))
        act = str(st.get("action", "")).lower()
        s = seed * 1000 + k
        ph = st.get("phase")
        if last_phase is not None and ph != last_phase:          # new layout: big transition
            ev.append((max(0.0, at - 0.05), whoosh(sr, 0.55, up=False, seed=s), 0.9))
            ev.append((max(0.0, at - 0.02), thump(sr, 0.5), 0.5))
        last_phase = ph
        if at - last_t < 0.11:                                   # avoid sound spam
            continue
        last_t = at
        r = _rng(s)
        if act in ("create", "grow"):
            ev.append((at, pop(sr, 480 + 220 * r.random(), rise=(act == "grow")), 0.8))
        elif act == "write":
            n = max(3, len(str(st.get("text", ""))))
            ev.append((at, scratch(sr, min(1.2, max(0.3, 0.05 * n)), seed=s), 0.8))
        elif act == "fade_in":
            ev.append((at, whoosh(sr, max(0.25, run), up=True, bright=0.5, seed=s), 0.45))
        elif act == "fade_out":
            ev.append((at, whoosh(sr, max(0.25, run), up=False, bright=0.5, seed=s), 0.4))
        elif act in ("move", "flow"):
            ev.append((at, whoosh(sr, max(0.3, run * 1.1), up=bool(r.random() > 0.5), seed=s), 0.75))
        elif act == "transform":
            ev.append((at, sweep(sr, max(0.3, min(0.6, run))), 0.7))
        elif act in ("highlight", "indicate"):
            ev.append((at, chime(sr, [660, 784, 880, 988][int(r.integers(0, 4))], 0.55, 0.7), 0.55))
        elif act == "flash":
            ev.append((at, chime(sr, 1175, 0.8, 1.3), 0.7))
        elif act == "count_up":
            m = int(max(6, min(16, run * 14)))
            for j in range(m):                                   # ticks that speed up
                tj = at + (j / m) ** 0.8 * run
                ev.append((tj, tick(sr, 1700 + 60 * j, seed=s + j), 0.5))
            ev.append((at + run, chime(sr, 1046, 0.5, 0.8), 0.5))
        elif act == "zoom_to":
            ev.append((at, whoosh(sr, max(0.4, run), up=True, bright=1.2, seed=s), 0.7))
            ev.append((at, thump(sr, 0.4, 120), 0.35))
        else:
            ev.append((at, pop(sr, 380, 0.14), 0.4))
    return ev


def _envelope(x, sr, win=0.03):
    k = max(1, int(sr * win))
    e = np.sqrt(np.convolve(x * x, np.ones(k, dtype=np.float32) / k, mode="same"))
    k2 = max(1, int(sr * 0.12))                                   # smooth so ducking is not twitchy
    return np.convolve(e, np.ones(k2, dtype=np.float32) / k2, mode="same")


def build_scene_mix(narration_wav, steps, out_wav, level=0.22, ambient=True, seed=0):
    """narration + sfx (+ quiet pad). `level` = sfx peak relative to narration peak."""
    nar, sr = read_wav(narration_wav)
    dur = len(nar) / sr
    sfx = np.zeros_like(nar)
    for at, snd, gain in _events(steps, dur, sr, seed):
        i = int(at * sr)
        if i >= len(sfx):
            continue
        j = min(len(sfx), i + len(snd))
        sfx[i:j] += snd[: j - i] * gain
    peak_n = float(np.max(np.abs(nar))) or 0.5
    if np.max(np.abs(sfx)) > 0:
        sfx *= (peak_n * level) / np.max(np.abs(sfx))
    if ambient:
        sfx += ambient_pad(sr, dur, seed) * peak_n * level * 0.12
    env = _envelope(nar, sr)
    env = env / (env.max() + 1e-9)
    sfx *= 1.0 - 0.5 * np.clip(env * 2.0, 0, 1)                    # duck under speech
    mix = nar + sfx
    m = float(np.max(np.abs(mix)))
    if m > 0.98:
        mix *= 0.98 / m
    write_wav(out_wav, mix, sr)
    return Path(out_wav)
