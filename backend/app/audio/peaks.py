"""Waveform peaks: one absolute peak (0..1) per bin, disk-cached per track.

Used by the seam editor (waveform drawing) and the seam suggester (energy
envelope). Decoding a whole track just for peaks is slow-ish, so results are
cached under data/cache/peaks, keyed by track id + resolution.
"""

import numpy as np

from .. import config
from ..models import WaveformOut
from . import decode


def get_or_compute(track_id: int, path: str, pps: int = 50) -> WaveformOut:
    """Peaks for a track at ~`pps` bins per second (may raise DecodeError)."""
    cache = config.CACHE_DIR / "peaks" / f"{track_id}_{pps}.json"
    if cache.exists():
        return WaveformOut.model_validate_json(cache.read_text(encoding="utf-8"))

    sr = config.ANALYSIS_SAMPLE_RATE
    audio = decode.decode(path, sample_rate=sr, mono=True)
    hop = sr // pps
    n_bins = -(-len(audio) // hop)  # ceil
    padded = np.pad(np.abs(audio), (0, n_bins * hop - len(audio)))
    peaks = np.minimum(padded.reshape(n_bins, hop).max(axis=1), 1.0)
    out = WaveformOut(
        track_id=track_id,
        bin_sec=hop / sr,
        duration_sec=len(audio) / sr,
        peaks=[round(float(p), 3) for p in peaks],
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Editor and suggester can request the same track concurrently; write
    # atomically so a reader never sees a half-written cache file.
    tmp = cache.with_suffix(f".{track_id}.tmp")
    tmp.write_text(out.model_dump_json(), encoding="utf-8")
    tmp.replace(cache)
    return out
