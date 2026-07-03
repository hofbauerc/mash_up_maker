"""Time-stretching via Rubber Band (bundled inside pedalboard).

TODO(render): tempo ramps across a seam for >10% BPM gaps (DESIGN.md #4) —
requires piecewise stretching with a changing factor, not a single call.
"""

import numpy as np


def stretch_to_bpm(audio: np.ndarray, sample_rate: int, from_bpm: float, to_bpm: float) -> np.ndarray:
    """Stretch audio so material at from_bpm plays at to_bpm (pitch preserved).

    Accepts (n,) mono or (n, 2) stereo; returns the same layout.
    """
    if abs(from_bpm - to_bpm) < 1e-6:
        return audio
    from pedalboard import time_stretch  # deferred heavy import

    # pedalboard expects (channels, samples); stretch_factor > 1 speeds up.
    mono = audio.ndim == 1
    buf = audio[np.newaxis, :] if mono else audio.T
    out = time_stretch(buf, sample_rate, stretch_factor=to_bpm / from_bpm)
    return out[0] if mono else out.T
