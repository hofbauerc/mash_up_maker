"""Content-aware EQ seeding: look at what actually plays in the window.

Instead of a one-size-fits-all bass swap, this reads the spectral band bins
(peaks.py) of the two window regions and generates EQ curves that fit the
material: the bass swap lands where the incoming kick really starts, a
kickless incoming intro gets no pointless bass-kill, and mids are only
dipped where both tracks genuinely fight. The output is plain CurvePoints
on the normal lanes — a starting point the user reshapes freely.
"""

from dataclasses import dataclass

import numpy as np

from ..models import CurvePoint, SeamParams, WaveformOut
from . import stems

_KICK_THRESH = 0.5  # a beat "has bass" above this fraction of the track's low reference
_KICK_SUSTAIN = 4  # beats the low band must stay up to count as the kick starting
_PRESENT_THRESH = 0.5  # per-band presence threshold vs. the track's own reference
_ABS_FLOOR = 0.02  # ~-34 dBFS: below this a band is silent no matter the reference
_MID_OVERLAP_MIN = 0.3  # dip mids only when both sides play them this much of the window
_MID_DIP_DB = -7.0
_SWAP_RAMP_BEATS = 2.0
_LATE_SWAP_BEATS = 16  # never hand the bass over earlier than this before the exit


@dataclass
class EQSeed:
    out_low: list[CurvePoint]
    out_mid: list[CurvePoint]
    in_low: list[CurvePoint]
    rationale: str


def seed_eq(
    out_wave: WaveformOut,
    in_wave: WaveformOut,
    out_bpm: float,
    in_bpm: float,
    params: SeamParams,
) -> EQSeed:
    """EQ curves for the current window geometry. Empty lanes mean "leave
    flat" — the manual-friendly default whenever nothing needs fixing."""
    if params.template != "blend":
        return EQSeed([], [], [], "cut — tracks never overlap, no EQ needed")
    if not out_wave.bands or not in_wave.bands:
        return _static_swap(params, "no spectral data yet — seeded the classic late bass swap")

    bb = params.blend_beats
    beat_out = 60.0 / out_bpm
    out_point = params.out_point_sec if params.out_point_sec is not None else out_wave.duration_sec
    out_profile = _window_profile(out_wave, out_point - bb * beat_out, out_point, bb)
    in_span = stems.in_window_span_sec(params, out_bpm, in_bpm)
    in_profile = _window_profile(in_wave, params.in_point_sec, params.in_point_sec + in_span, bb)

    out_ref = _references(out_wave)
    in_ref = _references(in_wave)
    notes: list[str] = []

    # --- Bass: put the swap where the incoming kick actually starts. ---
    out_has_bass = bool((out_profile[:, 0] >= max(_KICK_THRESH * out_ref[0], _ABS_FLOOR)).any())
    kick_b = _kick_onset_beat(in_profile[:, 0], in_ref[0])
    out_low: list[CurvePoint] = []
    in_low: list[CurvePoint] = []
    if kick_b is None:
        notes.append("incoming window has no kick/bass — no bass swap needed")
    elif not out_has_bass:
        notes.append(f"outgoing window is already bassless — incoming bass just enters (beat {kick_b:g})")
    else:
        swap_b = min(max(float(kick_b), bb - _LATE_SWAP_BEATS), bb - _SWAP_RAMP_BEATS)
        swap_b = max(_SWAP_RAMP_BEATS, round(swap_b / 4) * 4)  # keep it on the 4-beat grid
        ramp0 = max(0.0, swap_b - _SWAP_RAMP_BEATS)
        in_low = [CurvePoint(beat=0, value=-26)]
        if ramp0 > 0:
            in_low.append(CurvePoint(beat=ramp0, value=-26))
        in_low.append(CurvePoint(beat=swap_b, value=0))
        out_low = [CurvePoint(beat=ramp0, value=0), CurvePoint(beat=swap_b, value=-26)]
        notes.append(
            f"incoming kick from beat {kick_b:g} → bass swap at beat {swap_b:g}"
            + (" (held back to keep the outgoing groove)" if swap_b > kick_b else "")
        )

    # --- Mids: dip the outgoing only where the melodies really collide. ---
    out_mid: list[CurvePoint] = []
    both = (out_profile[:, 1] >= max(_PRESENT_THRESH * out_ref[1], _ABS_FLOOR)) & (
        in_profile[:, 1] >= max(_PRESENT_THRESH * in_ref[1], _ABS_FLOOR)
    )
    if both.mean() >= _MID_OVERLAP_MIN:
        first, last = float(np.argmax(both)), float(len(both) - 1 - np.argmax(both[::-1]))
        if first >= _SWAP_RAMP_BEATS:
            out_mid.append(CurvePoint(beat=first - _SWAP_RAMP_BEATS, value=0))
        out_mid += [CurvePoint(beat=first, value=_MID_DIP_DB), CurvePoint(beat=last, value=_MID_DIP_DB)]
        if last + _SWAP_RAMP_BEATS < bb:
            out_mid.append(CurvePoint(beat=last + _SWAP_RAMP_BEATS, value=0))
        notes.append(f"melodies overlap beats {first:g}–{last:g} → {_MID_DIP_DB:g} dB mid dip on outgoing")
    else:
        notes.append("no serious melody clash — mids left flat")

    return EQSeed(out_low=out_low, out_mid=out_mid, in_low=in_low, rationale="; ".join(notes))


def apply_seed(params: SeamParams, seed: EQSeed) -> SeamParams:
    """Copy of `params` with the EQ lanes replaced (volume/filter untouched)."""
    out = params.model_copy(deep=True)
    out.out_auto.eq_low_db = seed.out_low
    out.out_auto.eq_mid_db = seed.out_mid
    out.out_auto.eq_high_db = []
    out.in_auto.eq_low_db = seed.in_low
    out.in_auto.eq_mid_db = []
    out.in_auto.eq_high_db = []
    return out


def _window_profile(wave: WaveformOut, start_sec: float, end_sec: float, n_beats: int) -> np.ndarray:
    """(n_beats, 3) mean band level per window beat, sampled from the bins."""
    bands = np.asarray(wave.bands, dtype=np.float64)
    out = np.zeros((n_beats, 3))
    beat_sec = (end_sec - start_sec) / n_beats
    for b in range(n_beats):
        i0 = int((start_sec + b * beat_sec) / wave.bin_sec)
        i1 = max(i0 + 1, int((start_sec + (b + 1) * beat_sec) / wave.bin_sec))
        i0, i1 = np.clip([i0, i1], 0, len(bands) - 1 if len(bands) else 0)
        if i1 > i0:
            out[b] = bands[i0:i1].mean(axis=0)
    return out


def _references(wave: WaveformOut) -> np.ndarray:
    """Per-band "full-on" level: 95th percentile over the whole track."""
    bands = np.asarray(wave.bands, dtype=np.float64)
    refs = np.percentile(bands, 95, axis=0)
    return np.maximum(refs, 1e-6)


def _kick_onset_beat(low: np.ndarray, low_ref: float) -> int | None:
    """First window beat where the low band comes up and stays up."""
    hot = low >= max(_KICK_THRESH * low_ref, _ABS_FLOOR)
    run = min(_KICK_SUSTAIN, len(hot))
    for b in range(len(hot) - run + 1):
        if hot[b : b + run].all():
            return b
    return None


def _static_swap(params: SeamParams, why: str) -> EQSeed:
    """The pre-spectral fallback: trade basslines over the last 8 beats."""
    b = float(params.blend_beats)
    return EQSeed(
        out_low=[CurvePoint(beat=max(0.0, b - 8), value=0), CurvePoint(beat=b, value=-26)],
        out_mid=[],
        in_low=[
            CurvePoint(beat=0, value=-26),
            CurvePoint(beat=max(0.0, b - 8), value=-26),
            CurvePoint(beat=b, value=0),
        ],
        rationale=why,
    )
