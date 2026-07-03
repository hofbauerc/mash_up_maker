"""Seam preview segments: raw, tempo-matched audio around one seam.

Hybrid preview (DESIGN.md #8): the server renders only the *source* segments
— cut at the seam points and, for blends, with the incoming track stretched
to the outgoing tempo. All volume/EQ/filter/tail automation is applied live
by the client's Web Audio graph, so curve tweaks are instant and never
re-render. Segments are cached by a key over everything that does affect
them: cut points, window length, template and both tempi.

Preview timeline: t=0 is LEAD_BEATS before the transition window; the
outgoing segment spans [0, exit], the incoming segment starts at `entry_sec`
(window start for blends, the exit for cuts) and runs TAIL_BEATS past the
exit so there is context to judge the landing.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from .. import config
from ..models import SeamParams
from . import decode, stretch

LEAD_BEATS = 16
TAIL_BEATS = 32


@dataclass
class PreviewSegments:
    key: str
    out_path: Path
    in_path: Path
    tau0_sec: float  # preview t=0 in outgoing-track time
    entry_sec: float  # incoming entry, in preview time
    window_sec: float
    duration_sec: float


def render_segments(out_a: dict, in_a: dict, params: SeamParams) -> PreviewSegments:
    """out_a / in_a: analysis rows joined with track path/duration."""
    sr = config.RENDER_SAMPLE_RATE
    out_bpm, in_bpm = out_a["bpm"], in_a["bpm"]
    beat = 60.0 / out_bpm
    window_sec = params.blend_beats * beat
    out_point = (
        params.out_point_sec
        if params.out_point_sec is not None
        else (out_a["duration_sec"] or 0.0)
    )

    tau0 = max(0.0, out_point - window_sec - LEAD_BEATS * beat)
    entry_tau = out_point - window_sec if params.template == "blend" else out_point
    entry_sec = max(0.0, entry_tau - tau0)
    preview_end = (out_point - tau0) + TAIL_BEATS * beat
    # Source seconds of the incoming track needed to fill entry..preview_end
    # after stretching (blends play incoming material at the outgoing tempo).
    factor = out_bpm / in_bpm if params.template == "blend" else 1.0
    src_dur = (preview_end - entry_sec) * factor

    key = hashlib.sha1(
        f"{out_a['track_id']}:{in_a['track_id']}:{params.template}:{out_point:.3f}:"
        f"{params.in_point_sec:.3f}:{params.blend_beats}:{out_bpm}:{in_bpm}".encode()
    ).hexdigest()[:16]
    cache_dir = config.CACHE_DIR / "preview"
    out_path = cache_dir / f"{key}_out.wav"
    in_path = cache_dir / f"{key}_in.wav"

    if not (out_path.exists() and in_path.exists()):
        cache_dir.mkdir(parents=True, exist_ok=True)
        out_audio = decode.decode(out_a["path"], sample_rate=sr, mono=False)
        i0 = int(tau0 * sr)
        i1 = min(int(out_point * sr), len(out_audio))
        _write_wav(out_path, out_audio[i0:max(i0 + 1, i1)], sr)

        in_audio = decode.decode(in_a["path"], sample_rate=sr, mono=False)
        j0 = min(int(params.in_point_sec * sr), max(0, len(in_audio) - 1))
        j1 = min(j0 + int(src_dur * sr), len(in_audio))
        in_seg = in_audio[j0:max(j0 + 1, j1)]
        if params.template == "blend" and abs(out_bpm - in_bpm) > 1e-6:
            in_seg = stretch.stretch_to_bpm(in_seg, sr, from_bpm=in_bpm, to_bpm=out_bpm)
        _write_wav(in_path, in_seg, sr)

    duration = max(
        sf.info(str(out_path)).duration,
        entry_sec + sf.info(str(in_path)).duration,
    )
    return PreviewSegments(
        key=key,
        out_path=out_path,
        in_path=in_path,
        tau0_sec=round(tau0, 4),
        entry_sec=round(entry_sec, 4),
        window_sec=round(window_sec, 4),
        duration_sec=round(duration, 3),
    )


def _write_wav(path: Path, audio, sr: int) -> None:
    # 16-bit is plenty for preview and halves the transfer; write atomically
    # since two editor sessions could request the same seam concurrently.
    tmp = path.with_suffix(".tmp.wav")
    sf.write(tmp, audio, sr, subtype="PCM_16")
    tmp.replace(path)
