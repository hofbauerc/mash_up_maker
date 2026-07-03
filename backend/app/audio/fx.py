"""FX chains for the server-side render path (pedalboard).

Phase 1 scaffold ships static chains only. The seam editor needs these to be
automatable over time (EQ curves, filter sweeps) — that means chunked
processing with per-chunk parameter updates.

TODO(fx): time-automation engine — process in ~1024-sample chunks, interpolate
parameter values per chunk from the seam's automation curves. Keep parameters
limited to what Web Audio can mirror live (gain, biquad, delay, convolver) so
the client preview stays honest (DESIGN.md #8, risk #1).
TODO(fx): reverb/delay "tail" helper — feed the outgoing track's last beats
into the FX and let the tail ring out under the incoming track.
"""

import numpy as np


def three_band_eq(low_db: float = 0.0, mid_db: float = 0.0, high_db: float = 0.0):
    """Static 3-band EQ (low shelf @200Hz, peak @1.2kHz, high shelf @6kHz)."""
    from pedalboard import HighShelfFilter, LowShelfFilter, Pedalboard, PeakFilter

    return Pedalboard([
        LowShelfFilter(cutoff_frequency_hz=200, gain_db=low_db),
        PeakFilter(cutoff_frequency_hz=1200, gain_db=mid_db),
        HighShelfFilter(cutoff_frequency_hz=6000, gain_db=high_db),
    ])


def apply(board, audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Run a pedalboard chain over (n,) or (n, 2) audio."""
    mono = audio.ndim == 1
    buf = audio[np.newaxis, :] if mono else audio.T
    out = board(buf, sample_rate)
    return out[0] if mono else out.T
