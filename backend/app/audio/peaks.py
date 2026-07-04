"""Waveform peaks + spectral bands, disk-cached per track.

Per bin: absolute peak (0..1) for drawing, full-band RMS for loudness
matching, and low/mid/high band RMS (crossovers ~200 Hz / ~4 kHz, matching
the seam EQ's shelf regions) for the spectral waveform display and
content-aware auto-EQ. Decoding a whole track just for this is slow-ish, so
results are cached under data/cache/peaks, keyed by track id + resolution;
caches written before the bands existed are transparently recomputed.
"""

import numpy as np

from .. import config
from ..models import WaveformOut
from . import decode

BAND_EDGES_HZ = (200.0, 4000.0)  # low | mid | high crossovers


def get_or_compute(track_id: int, path: str, pps: int = 50) -> WaveformOut:
    """Peaks for a track at ~`pps` bins per second (may raise DecodeError)."""
    cache = config.CACHE_DIR / "peaks" / f"{track_id}_{pps}.json"
    if cache.exists():
        cached = WaveformOut.model_validate_json(cache.read_text(encoding="utf-8"))
        if cached.bands is not None and cached.rms is not None:
            return cached
        # pre-spectral cache: fall through and recompute

    sr = config.ANALYSIS_SAMPLE_RATE
    audio = decode.decode(path, sample_rate=sr, mono=True)
    hop = sr // pps
    n_bins = -(-len(audio) // hop)  # ceil
    pad = n_bins * hop - len(audio)

    def binned(x: np.ndarray) -> np.ndarray:
        return np.pad(x, (0, pad)).reshape(n_bins, hop)

    peaks = np.minimum(binned(np.abs(audio)).max(axis=1), 1.0)
    rms = np.sqrt(binned(audio.astype(np.float64) ** 2).mean(axis=1))
    bands = [np.sqrt(binned(b**2).mean(axis=1)) for b in _split_bands(audio, sr)]

    out = WaveformOut(
        track_id=track_id,
        bin_sec=hop / sr,
        duration_sec=len(audio) / sr,
        peaks=[round(float(p), 3) for p in peaks],
        rms=[round(float(v), 4) for v in rms],
        bands=[[round(float(b[i]), 4) for b in bands] for i in range(n_bins)],
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Editor and suggester can request the same track concurrently; write
    # atomically so a reader never sees a half-written cache file.
    tmp = cache.with_suffix(f".{track_id}.tmp")
    tmp.write_text(out.model_dump_json(), encoding="utf-8")
    tmp.replace(cache)
    return out


def loudness_db(wave: WaveformOut) -> float | None:
    """Perceived level of the track's loud sections: 95th-percentile bin RMS
    in dBFS. Hard-dance masters differ mostly in how hot their full-on
    sections are, which is exactly what a set-trim should equalize."""
    if not wave.rms:
        return None
    active = np.asarray([v for v in wave.rms if v > 1e-4])
    if active.size == 0:
        return None
    return float(20 * np.log10(np.percentile(active, 95)))


def _split_bands(audio: np.ndarray, sr: int) -> list[np.ndarray]:
    """Split into low/mid/high via 4th-order Butterworth crossovers."""
    from scipy.signal import butter, sosfilt

    lo_edge, hi_edge = BAND_EDGES_HZ
    x = audio.astype(np.float64)
    low = sosfilt(butter(4, lo_edge, btype="lowpass", fs=sr, output="sos"), x)
    mid = sosfilt(butter(4, [lo_edge, hi_edge], btype="bandpass", fs=sr, output="sos"), x)
    high = sosfilt(butter(4, hi_edge, btype="highpass", fs=sr, output="sos"), x)
    return [low, mid, high]
