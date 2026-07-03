"""Per-track analysis: BPM, constant beat grid, key -> Camelot, energy, sections.

Hard dance is produced on a rigid grid, so the beat model is deliberately
simple: one BPM plus one anchor offset per track (DESIGN.md defaults). Manual
correction in the UI is the backstop for the rare track this gets wrong.
"""

from dataclasses import dataclass, field

import numpy as np

from .. import config
from ..models import Section
from . import decode

# Krumhansl-Schmuckler key profiles (major / minor).
_KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

_PITCH_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]

# Camelot wheel, keyed by (pitch_class, mode). Mode: "major" | "minor".
_CAMELOT = {
    (11, "major"): "1B", (8, "minor"): "1A",
    (6, "major"): "2B", (3, "minor"): "2A",
    (1, "major"): "3B", (10, "minor"): "3A",
    (8, "major"): "4B", (5, "minor"): "4A",
    (3, "major"): "5B", (0, "minor"): "5A",
    (10, "major"): "6B", (7, "minor"): "6A",
    (5, "major"): "7B", (2, "minor"): "7A",
    (0, "major"): "8B", (9, "minor"): "8A",
    (7, "major"): "9B", (4, "minor"): "9A",
    (2, "major"): "10B", (11, "minor"): "10A",
    (9, "major"): "11B", (6, "minor"): "11A",
    (4, "major"): "12B", (1, "minor"): "12A",
}


@dataclass
class AnalysisResult:
    bpm: float
    beat_offset_sec: float
    key_name: str | None
    camelot: str | None
    energy: float | None
    sections: list[Section] = field(default_factory=list)


def analyze_track(path: str) -> AnalysisResult:
    import librosa  # deferred: heavy import, keep app startup fast

    sr = config.ANALYSIS_SAMPLE_RATE
    y = decode.decode(path, sample_rate=sr, mono=True)
    if y.size < sr * 5:
        raise ValueError("track shorter than 5 seconds, refusing to analyze")

    bpm, beat_offset = _beat_grid(y, sr)
    key_name, camelot = _detect_key(y, sr)
    energy = _energy(y)
    sections = _detect_sections(y, sr)
    return AnalysisResult(
        bpm=bpm,
        beat_offset_sec=beat_offset,
        key_name=key_name,
        camelot=camelot,
        energy=energy,
        sections=sections,
    )


def _beat_grid(y: np.ndarray, sr: int) -> tuple[float, float]:
    """Fit a constant grid: BPM + anchor offset of beat 0."""
    import librosa

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, trim=False)
    bpm = float(np.atleast_1d(tempo)[0])
    # Half-time detection is the classic failure mode on 150+ BPM material.
    while 0 < bpm < config.MIN_PLAUSIBLE_BPM:
        bpm *= 2.0
    bpm = round(bpm, 2)

    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    if len(beat_times) == 0:
        return bpm, 0.0
    # Anchor the grid: circular median of beat positions modulo one period.
    period = 60.0 / bpm
    phases = np.mod(beat_times, period)
    angles = phases / period * 2 * np.pi
    mean_angle = np.arctan2(np.sin(angles).mean(), np.cos(angles).mean())
    offset = (mean_angle % (2 * np.pi)) / (2 * np.pi) * period
    return bpm, float(round(offset, 4))


def _detect_key(y: np.ndarray, sr: int) -> tuple[str | None, str | None]:
    import librosa

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    if not np.any(chroma):
        return None, None
    best: tuple[float, int, str] | None = None
    for mode, profile in (("major", _KS_MAJOR), ("minor", _KS_MINOR)):
        for pc in range(12):
            r = float(np.corrcoef(np.roll(profile, pc), chroma)[0, 1])
            if best is None or r > best[0]:
                best = (r, pc, mode)
    _, pc, mode = best
    key_name = f"{_PITCH_NAMES[pc]} {mode}"
    return key_name, _CAMELOT.get((pc, mode))


def _energy(y: np.ndarray) -> float:
    """Loudness proxy in [0, 1]: 95th-percentile short-window RMS."""
    import librosa

    rms = librosa.feature.rms(y=y)[0]
    return float(np.clip(np.percentile(rms, 95) * 2.0, 0.0, 1.0))


def _detect_sections(y: np.ndarray, sr: int) -> list[Section]:
    """Crude energy-threshold segmentation.

    High-RMS regions become 'drop', low-RMS regions 'break'; leading/trailing
    low regions become 'intro'/'outro'. Good enough to anchor seam suggestions.
    TODO(analysis): replace with novelty-based segmentation + build detection,
    and let the user relabel sections in the UI (DESIGN.md risk #2).
    """
    import librosa

    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    if rms.size == 0:
        return []
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    high = rms > (np.median(rms) * 1.1)

    sections: list[Section] = []
    start_idx = 0
    for i in range(1, len(high) + 1):
        if i == len(high) or high[i] != high[start_idx]:
            start_t, end_t = float(times[start_idx]), float(times[min(i, len(times) - 1)])
            if end_t - start_t >= 8.0:  # ignore blips shorter than ~8s
                sections.append(
                    Section(label="drop" if high[start_idx] else "break", start_sec=start_t, end_sec=end_t)
                )
            start_idx = i
    if sections and sections[0].label == "break":
        sections[0].label = "intro"
    if sections and sections[-1].label == "break":
        sections[-1].label = "outro"
    return sections
