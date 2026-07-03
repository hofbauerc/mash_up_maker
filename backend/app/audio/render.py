"""Set rendering: source of truth for what a project sounds like.

Grid-aware render honoring the full SeamParams (DESIGN.md #4, #8):

- Blends tempo-match the incoming track to the outgoing BPM across the
  transition window (both seam points are beat-snapped by the UI, so kicks
  align), then ramp it back to its native tempo over the next RAMP_BEATS —
  one-beat Rubber Band chunks whose boundaries hide under kick onsets.
- Volume / 3-band EQ / filter-sweep automation runs through fx.py, which
  mirrors the client preview graph node for node; the preview stays honest
  and this render stays the ground truth (risk #1).
- Reverb/delay tails ring out past the exit, under the incoming track.
- Cuts are sample-exact with a ~1.5 ms declick fade on each side.

Automation curves act on the seam window (out side: also the 16-beat lead
the preview plays, with edge values held). Values a curve holds at the
window end simply stop applying there — the transition owns the window,
the track body plays clean.

Tracks are decoded, processed and folded into the mix one at a time; only
the still-overlappable tail of the mix is kept as a working buffer, so peak
memory stays near two tracks, not the whole set.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .. import config
from ..models import Project, SeamParams, SideAutomation
from . import decode, fx, stretch
from .preview import LEAD_BEATS

RAMP_BEATS = 32  # post-blend tempo ramp back to the incoming track's native BPM
DECLICK_SEC = 0.0015
_STRETCH_EPS = 0.001  # |factor - 1| below this: not worth stretching


@dataclass
class RenderedSet:
    wav_path: Path
    mp3_path: Path | None
    tracklist_path: Path
    duration_sec: float


def render_set(project: Project, track_rows: list[dict], out_dir: Path) -> RenderedSet:
    """Render a project to WAV + MP3 + timestamped tracklist.

    track_rows: one dict per project track in set order, each carrying
    id, path, filename, bpm and beat_offset_sec (analysis is required).
    """
    sr = config.RENDER_SAMPLE_RATE
    if not track_rows:
        raise ValueError("project has no tracks")

    seam_by_pair = {(s.out_track_id, s.in_track_id): s.params for s in project.seams}
    seams: list[SeamParams] = [
        seam_by_pair.get((a["id"], b["id"]), SeamParams())
        for a, b in zip(track_rows, track_rows[1:])
    ]
    # Transition window per seam, in output samples at the outgoing tempo.
    win_smp = [
        int(round(p.blend_beats * 60.0 / out_row["bpm"] * sr))
        for p, out_row in zip(seams, track_rows)
    ]

    done: list[np.ndarray] = []
    work = np.zeros((0, 2), dtype=np.float32)  # mix from work_start onward
    work_start = 0  # in samples
    stream_start = 0
    start_times: list[float] = []

    for i, row in enumerate(track_rows):
        audio = decode.decode(row["path"], sample_rate=sr, mono=False)
        prev = (seams[i - 1], track_rows[i - 1]["bpm"], win_smp[i - 1]) if i > 0 else None
        nxt = (seams[i], row["bpm"], win_smp[i]) if i < len(seams) else None

        stream = _build_stream(audio, sr, row["bpm"], prev, nxt)
        exit_len = len(stream)
        stream = _apply_seam_sides(stream, sr, prev, nxt)

        offset = stream_start - work_start
        need = offset + len(stream)
        if need > len(work):
            work = np.concatenate([work, np.zeros((need - len(work), 2), dtype=np.float32)])
        work[offset : offset + len(stream)] += stream
        start_times.append(stream_start / sr)

        if nxt is not None:
            params, _, w = nxt
            exit_smp = stream_start + exit_len
            entry_next = exit_smp - w if params.template == "blend" else exit_smp
            entry_next = max(entry_next, stream_start)
            # Everything before the next entry can no longer be overlapped.
            cut = min(max(entry_next - work_start, 0), len(work))
            done.append(work[:cut])
            work = work[cut:]
            work_start += cut
            stream_start = entry_next

    mix = np.concatenate([*done, work]) if done else work
    peak = float(np.abs(mix).max(initial=0.0))
    if peak > 0.999:  # summed blends / EQ boosts can poke over full scale
        mix *= 0.999 / peak

    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{project.name}.wav"
    sf.write(wav_path, mix, sr, subtype="FLOAT")

    mp3_path = out_dir / f"{project.name}.mp3"
    if not _encode_mp3(wav_path, mp3_path):
        mp3_path = None

    tracklist_path = out_dir / f"{project.name}_tracklist.txt"
    tracklist_path.write_text(
        "\n".join(
            f"{_fmt_time(t)}  {row['filename']}" for t, row in zip(start_times, track_rows)
        )
        + "\n",
        encoding="utf-8",
    )
    return RenderedSet(wav_path, mp3_path, tracklist_path, duration_sec=len(mix) / sr)


def _build_stream(
    audio: np.ndarray,
    sr: int,
    own_bpm: float,
    prev: tuple[SeamParams, float, int] | None,
    nxt: tuple[SeamParams, float, int] | None,
) -> np.ndarray:
    """This track's contribution at output rate: from its entry point up to
    its exit point, tempo-matched at the start when it enters via a blend."""
    in_point = prev[0].in_point_sec if prev else 0.0
    out_point = nxt[0].out_point_sec if nxt else None
    i0 = min(max(int(round(in_point * sr)), 0), len(audio) - 1)
    i1 = len(audio) if out_point is None else min(max(int(round(out_point * sr)), i0 + 1), len(audio))

    factor = 1.0
    if prev is not None and prev[0].template == "blend":
        factor = prev[1] / own_bpm  # play incoming material at the outgoing tempo
    if abs(factor - 1.0) < _STRETCH_EPS:
        return audio[i0:i1].copy()

    _, _, w = prev
    win_src_end = min(i0 + int(round(w * factor)), i1)
    spans = [(i0, win_src_end, factor)]
    spans += stretch.ramp_spans(win_src_end, 60.0 / own_bpm * sr, RAMP_BEATS, factor, i1)
    body_start = spans[-1][1]
    if body_start < i1:
        spans.append((body_start, i1, 1.0))
    return np.ascontiguousarray(stretch.stretch_chain(audio, sr, spans), dtype=np.float32)


def _apply_seam_sides(
    stream: np.ndarray,
    sr: int,
    prev: tuple[SeamParams, float, int] | None,
    nxt: tuple[SeamParams, float, int] | None,
) -> np.ndarray:
    """Automate the incoming window at the stream head and the outgoing
    window (+ preview lead) at its tail; append the tail-FX ring-out."""
    if prev is not None:
        params, out_bpm, w = prev
        beat = 60.0 / out_bpm  # curve beats are outgoing-track beats on both sides
        n = min(len(stream), w)
        if n > 0:
            vol = _volume_env(n, sr, params, params.in_auto, "in", beat, win_start_sec=0.0)
            head = fx.process_side(stream[:n], sr, params.in_auto, beat, 0.0, vol)
            if params.template == "cut":
                _declick(head, sr, "in")
            stream = np.concatenate([head, stream[n:]])

    if nxt is not None:
        params, own_bpm, w = nxt
        beat = 60.0 / own_bpm
        n = min(len(stream), w + int(round(LEAD_BEATS * beat * sr)))
        if n > 0:
            win_start = n / sr - w / sr
            vol = _volume_env(n, sr, params, params.out_auto, "out", beat, win_start)
            body = fx.process_side(stream[len(stream) - n :], sr, params.out_auto, beat, win_start, vol)
            if params.template == "cut":
                _declick(body, sr, "out")
            wet = fx.render_tail(body, sr, params.tail, beat)
            stream = np.concatenate([stream[: len(stream) - n], body])
            if wet is not None:
                total = max(len(stream), len(stream) - n + len(wet))
                mixed = np.zeros((total, 2), dtype=np.float32)
                mixed[: len(stream)] = stream
                mixed[len(stream) - n : len(stream) - n + len(wet)] += wet
                stream = mixed
    return stream


def _volume_env(
    n: int,
    sr: int,
    params: SeamParams,
    auto: SideAutomation,
    side: str,
    beat_sec: float,
    win_start_sec: float,
) -> np.ndarray | None:
    """Per-sample gain: the drawn curve, else the template default (equal-power
    fade for blends, unity for cuts — matching previewEngine.ts)."""
    beats = (np.arange(n, dtype=np.float64) / sr - win_start_sec) / beat_sec
    env = fx.curve_values(auto.volume, beats)
    if env is not None:
        return env
    if params.template != "blend":
        return None
    x = np.clip(beats / params.blend_beats, 0.0, 1.0) * (np.pi / 2)
    return np.cos(x) if side == "out" else np.sin(x)


def _declick(region: np.ndarray, sr: int, side: str) -> None:
    """~1.5 ms fade so sample-exact cut edges never click. In place."""
    n = min(len(region), max(1, int(DECLICK_SEC * sr)))
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, np.newaxis]
    if side == "in":
        region[:n] *= ramp
    else:
        region[len(region) - n :] *= ramp[::-1]


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
