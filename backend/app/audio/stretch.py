"""Time-stretching via Rubber Band (bundled inside pedalboard).

`stretch_to_bpm` is the single-factor call used for preview segments and the
blend window. `stretch_chain` runs consecutive spans of one source at
different factors and joins them click-free — that is how a blended-in track
ramps from the outgoing tempo back to its native tempo after a seam
(DESIGN.md #4). A dedicated ramp *transition* for >10% BPM gaps (tempo ramp
across the seam itself) stays future work; cuts cover those gaps in Phase 1.
"""

import numpy as np


def stretch_to_bpm(audio: np.ndarray, sample_rate: int, from_bpm: float, to_bpm: float) -> np.ndarray:
    """Stretch audio so material at from_bpm plays at to_bpm (pitch preserved).

    Accepts (n,) mono or (n, 2) stereo; returns the same layout.
    """
    if abs(from_bpm - to_bpm) < 1e-6:
        return audio
    return _stretch(audio, sample_rate, to_bpm / from_bpm)


def stretch_chain(
    audio: np.ndarray,
    sample_rate: int,
    spans: list[tuple[int, int, float]],
    xfade_sec: float = 0.005,
) -> np.ndarray:
    """Play consecutive `[start, end)` sample spans of `audio` at per-span
    stretch factors (>1 is faster) and concatenate the results.

    Each span is stretched independently, so joins would jump in phase; to
    keep them click-free every span is stretched with a little extra source
    context and linearly crossfaded (~5 ms) into the next span's start.
    Output length is Σ round((end-start)/factor) exactly — beat positions
    stay on the grid the caller computed.
    """
    xf = max(0, int(round(xfade_sec * sample_rate)))
    pieces: list[np.ndarray] = []
    carry: np.ndarray | None = None  # stretched context past the previous span
    for start, end, factor in spans:
        target = int(round((end - start) / factor))
        extra_src = min(len(audio) - end, int(np.ceil(xf * factor)))
        seg = audio[start : end + extra_src]
        stretched = seg if abs(factor - 1.0) < 1e-3 else _stretch(seg, sample_rate, factor)
        want = target + int(round(extra_src / factor))
        stretched = _fix_length(stretched, want)
        head, ctx = stretched[:target].copy(), stretched[target:]
        if carry is not None and len(carry) and len(head):
            n = min(len(carry), len(head))
            # Linear crossfade: the two sides are the same material, so a
            # correlated (linear) fade avoids the +3 dB equal-power bump.
            t = np.linspace(0.0, 1.0, n, dtype=np.float32)
            shape = (n,) if head.ndim == 1 else (n, 1)
            head[:n] = head[:n] * t.reshape(shape) + carry[:n] * (1.0 - t.reshape(shape))
        pieces.append(head)
        carry = ctx
    if not pieces:
        return audio[:0]
    return np.concatenate(pieces)


def ramp_spans(
    start_idx: int,
    native_beat_samples: float,
    n_beats: int,
    from_factor: float,
    total_len: int,
) -> list[tuple[int, int, float]]:
    """Spans for a tempo ramp: `n_beats` one-beat chunks whose factor eases
    linearly from `from_factor` to 1.0. Chunk edges land on the source's own
    beats, so any residual stretch artifacts hide under kick onsets."""
    spans: list[tuple[int, int, float]] = []
    for k in range(n_beats):
        a = start_idx + int(round(k * native_beat_samples))
        b = start_idx + int(round((k + 1) * native_beat_samples))
        if a >= total_len:
            break
        f = from_factor + (k + 0.5) / n_beats * (1.0 - from_factor)
        spans.append((a, min(b, total_len), f))
    return spans


def _stretch(audio: np.ndarray, sample_rate: int, factor: float) -> np.ndarray:
    from pedalboard import time_stretch  # deferred heavy import

    # pedalboard expects (channels, samples); stretch_factor > 1 speeds up.
    mono = audio.ndim == 1
    buf = audio[np.newaxis, :] if mono else np.ascontiguousarray(audio.T)
    out = time_stretch(buf, sample_rate, stretch_factor=factor)
    return out[0] if mono else out.T


def _fix_length(audio: np.ndarray, n: int) -> np.ndarray:
    if len(audio) == n:
        return audio
    if len(audio) > n:
        return audio[:n]
    pad = [(0, n - len(audio))] + [(0, 0)] * (audio.ndim - 1)
    return np.pad(audio, pad)
