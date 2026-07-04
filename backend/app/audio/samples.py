"""Built-in sample pack: procedurally synthesized transition one-shots.

Phase 1.5 (DESIGN.md): risers, impacts, crashes and white-noise sweeps the
user places on the seam's beat grid. Everything is synthesized from seeded
noise/oscillators — no bundled audio, nothing to license — so a given
(kind, bpm, beats) always produces the identical waveform for the browser
preview and the server render.

Beat-synced kinds (riser, noise) are generated at the outgoing track's tempo
so they span an exact number of beats and land on the drop; impact and crash
are fixed-length one-shots. Synthesized WAVs are cached under
data/cache/samples/ for the preview endpoint; the render path synthesizes
in-memory.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .. import config
from . import fx

_DECLICK_SEC = 0.005  # end fade so a sample cut off at full level never clicks
_SEEDS = {"riser": 101, "noise": 202, "impact": 303, "crash": 404}


@dataclass(frozen=True)
class SampleKind:
    kind: str
    label: str
    beat_synced: bool


KINDS: dict[str, SampleKind] = {
    "riser": SampleKind("riser", "Riser", beat_synced=True),
    "noise": SampleKind("noise", "Noise sweep", beat_synced=True),
    "impact": SampleKind("impact", "Impact", beat_synced=False),
    "crash": SampleKind("crash", "Crash", beat_synced=False),
}


def synthesize(
    kind: str, bpm: float, beats: float, sr: int = config.RENDER_SAMPLE_RATE
) -> np.ndarray:
    """Deterministic (n, 2) float32 sample, peak-normalized to 1.0."""
    spec = KINDS[kind]
    rng = np.random.default_rng(_SEEDS[kind])
    if spec.beat_synced:
        bpm = float(np.clip(bpm, 60.0, 300.0))
        beats = float(np.clip(beats, 1.0, 128.0))
        n = max(1, int(round(beats * 60.0 / bpm * sr)))
        out = _riser(n, sr, rng) if kind == "riser" else _noise(n, sr, rng)
    else:
        out = _impact(sr, rng) if kind == "impact" else _crash(sr, rng)

    peak = float(np.abs(out).max(initial=0.0))
    if peak > 0:
        out /= peak
    fade = min(len(out), max(1, int(_DECLICK_SEC * sr)))
    out[-fade:] *= np.linspace(1.0, 0.0, fade)[:, np.newaxis]
    return np.ascontiguousarray(out, dtype=np.float32)


def ensure_wav(kind: str, bpm: float, beats: float) -> Path:
    """Synthesize into the cache (if missing) and return the WAV path."""
    spec = KINDS[kind]
    cache_dir = config.CACHE_DIR / "samples"
    if spec.beat_synced:
        bpm = float(np.clip(bpm, 60.0, 300.0))
        beats = float(np.clip(beats, 1.0, 128.0))
        path = cache_dir / f"{kind}_{bpm:.2f}bpm_{beats:g}.wav"
    else:
        path = cache_dir / f"{kind}.wav"
    if not path.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.wav")  # atomic like preview segments
        sf.write(tmp, synthesize(kind, bpm, beats), config.RENDER_SAMPLE_RATE, subtype="FLOAT")
        tmp.replace(path)
    return path


def _sweep_filter(x: np.ndarray, sr: int, kind: str, f0: float, f1: float) -> np.ndarray:
    """Exponential cutoff glide f0 -> f1 across the buffer, chunked like
    fx.process_side so it behaves like the preview's a-rate AudioParams."""
    biq = fx._Biquad(kind, sr, x.shape[1])
    out = np.empty_like(x)
    n = len(x)
    for start in range(0, n, fx.CHUNK):
        end = min(start + fx.CHUNK, n)
        pos = (start + end) / 2 / n
        out[start:end] = biq.process(x[start:end], f0 * (f1 / f0) ** pos)
    return out


def _detuned_saw(n: int, sr: int, f0: float, f1: float) -> np.ndarray:
    """Stereo saw gliding f0 -> f1 exponentially, channels detuned for width."""
    t = np.arange(n) / max(n, 1)
    freq = f0 * (f1 / f0) ** t
    chans = []
    for det in (0.996, 1.004):
        phase = np.cumsum(freq * det) / sr
        chans.append(2.0 * (phase % 1.0) - 1.0)
    return np.column_stack(chans)


def _riser(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """Opening-lowpass noise + rising detuned saw, swelling into the drop."""
    t = np.arange(n) / max(n, 1)
    noise = _sweep_filter(rng.uniform(-1.0, 1.0, (n, 2)), sr, "lowpass", 500.0, 14000.0)
    env = (0.12 + 0.88 * t**2)[:, np.newaxis]
    tone = _detuned_saw(n, sr, 110.0, 440.0)
    return (0.75 * noise + 0.3 * tone) * env


def _noise(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    """Smooth white-noise bed with a slow lowpass sweep upward."""
    t = np.arange(n) / max(n, 1)
    out = _sweep_filter(rng.uniform(-1.0, 1.0, (n, 2)), sr, "lowpass", 250.0, 16000.0)
    return out * (0.35 + 0.65 * t**1.5)[:, np.newaxis]


def _impact(sr: int, rng: np.random.Generator) -> np.ndarray:
    """Sub thump (120 -> 40 Hz pitch drop) plus a short dark noise burst."""
    n = int(2.5 * sr)
    ts = np.arange(n) / sr
    freq = 40.0 + 80.0 * np.exp(-ts / 0.09)
    sub = np.sin(2 * np.pi * np.cumsum(freq) / sr) * np.exp(-ts / 0.6)
    burst = rng.uniform(-1.0, 1.0, (n, 2)) * np.exp(-ts / 0.06)[:, np.newaxis]
    burst = _sweep_filter(burst, sr, "lowpass", 4000.0, 4000.0)
    return 0.95 * np.column_stack([sub, sub]) + 0.5 * burst


def _crash(sr: int, rng: np.random.Generator) -> np.ndarray:
    """Bright decorrelated noise wash with an exponential decay."""
    n = int(3.0 * sr)
    ts = np.arange(n) / sr
    out = _sweep_filter(rng.uniform(-1.0, 1.0, (n, 2)), sr, "highpass", 2500.0, 2500.0)
    return out * np.exp(-ts / 0.8)[:, np.newaxis]
