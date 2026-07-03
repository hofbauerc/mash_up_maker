"""Set rendering: source of truth for what a project sounds like.

Phase 1 scaffold renders a *naive* but complete set: hard cuts and equal-power
crossfades at the chosen points, no tempo matching, no EQ/FX automation yet.
This makes export work end to end from day one; quality comes next.

TODO(render): tempo-match the incoming track to the outgoing BPM across the
blend region (stretch.py), snapping both to their beat grids (DESIGN.md #4).
TODO(render): apply per-seam EQ/volume curves and FX from SeamParams (fx.py).
TODO(render): stream instead of holding the whole set in memory — a 60-minute
set at 44.1k stereo float32 is ~1.2 GB.

The hybrid preview's segment rendering lives in preview.py; once this module
is grid-aware it should share the tempo-matching code path with it so preview
and export stay aligned (DESIGN.md risk #1).
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .. import config
from ..models import Project, SeamParams
from . import decode


@dataclass
class RenderedSet:
    wav_path: Path
    mp3_path: Path | None
    tracklist_path: Path
    duration_sec: float


@dataclass
class _TrackSource:
    track_id: int
    filename: str
    audio: np.ndarray  # (n, 2) float32 at RENDER_SAMPLE_RATE


def render_set(project: Project, track_rows: list[dict], out_dir: Path) -> RenderedSet:
    """Render a project to WAV + MP3 + timestamped tracklist.

    track_rows: one dict per project track (id, path, filename), in set order.
    """
    sr = config.RENDER_SAMPLE_RATE
    sources = [
        _TrackSource(r["id"], r["filename"], decode.decode(r["path"], sample_rate=sr, mono=False))
        for r in track_rows
    ]
    if not sources:
        raise ValueError("project has no tracks")

    seam_by_pair = {(s.out_track_id, s.in_track_id): s.params for s in project.seams}

    mix = sources[0].audio
    start_times = [0.0]  # start of each track within the mix
    for prev, nxt in zip(sources, sources[1:]):
        params = seam_by_pair.get((prev.track_id, nxt.track_id), SeamParams())
        mix, entry_sec = _append_with_seam(mix, nxt.audio, params, sr)
        start_times.append(entry_sec)

    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{project.name}.wav"
    sf.write(wav_path, mix, sr, subtype="FLOAT")

    mp3_path = out_dir / f"{project.name}.mp3"
    if not _encode_mp3(wav_path, mp3_path):
        mp3_path = None

    tracklist_path = out_dir / f"{project.name}_tracklist.txt"
    tracklist_path.write_text(
        "\n".join(
            f"{_fmt_time(t)}  {src.filename}" for t, src in zip(start_times, sources)
        )
        + "\n",
        encoding="utf-8",
    )
    return RenderedSet(wav_path, mp3_path, tracklist_path, duration_sec=len(mix) / sr)


def _append_with_seam(
    mix: np.ndarray, incoming: np.ndarray, params: SeamParams, sr: int
) -> tuple[np.ndarray, float]:
    """Join `incoming` onto `mix` per seam params; returns (mix, entry_time_sec)."""
    out_end = int((params.out_point_sec or (len(mix) / sr)) * sr)
    out_end = min(max(out_end, 1), len(mix))
    in_start = int(params.in_point_sec * sr)
    incoming = incoming[min(in_start, len(incoming) - 1):]

    if params.template == "cut":
        mix = np.concatenate([mix[:out_end], incoming])
        return mix, out_end / sr

    # blend: equal-power crossfade ending at out_end.
    # Naive placeholder — blend_beats is interpreted at 150 BPM until the
    # renderer is grid-aware (TODO(render) above).
    fade_len = min(int(params.blend_beats * 60 / 150 * sr), out_end, len(incoming))
    t = np.linspace(0.0, np.pi / 2, fade_len, dtype=np.float32)[:, np.newaxis]
    entry = out_end - fade_len
    overlap = mix[entry:out_end] * np.cos(t) + incoming[:fade_len] * np.sin(t)
    mix = np.concatenate([mix[:entry], overlap, incoming[fade_len:]])
    return mix, entry / sr


def _encode_mp3(wav_path: Path, mp3_path: Path) -> bool:
    proc = subprocess.run(
        [config.FFMPEG, "-v", "error", "-y", "-i", str(wav_path), "-b:a", "320k", str(mp3_path)],
        capture_output=True,
    )
    return proc.returncode == 0


def _fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}"
