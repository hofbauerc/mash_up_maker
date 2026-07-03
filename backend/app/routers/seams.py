"""Seam endpoints: transition suggestion + preview segments."""

import json
import math
import re

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config, db
from ..audio import decode, peaks
from ..audio import preview as preview_audio
from ..models import CurvePoint, SeamParams, SeamPreviewOut, SeamSuggestion

router = APIRouter(prefix="/api/seams", tags=["seams"])


class SeamPairIn(BaseModel):
    out_track_id: int
    in_track_id: int


def _analysis(conn, track_id: int) -> dict:
    row = conn.execute(
        """SELECT t.duration_sec, t.path, a.* FROM tracks t
           JOIN analysis a ON a.track_id = t.id WHERE t.id=?""",
        (track_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(409, f"track {track_id} not analyzed yet")
    return dict(row)


def _last_full_energy_sec(peak_bins: list[float], bin_sec: float, bpm: float) -> float | None:
    """Last time the track still plays at "full" level: >= 80% of its loud
    reference (95th percentile) on a ~2-beat smoothed envelope of |peak| bins.

    Hard-dance outros drop well below the kick-section level, so this lands at
    the end of the last kick section even when section labels are wrong.
    """
    if not peak_bins:
        return None
    env = np.asarray(peak_bins, dtype=np.float32)
    win = max(1, round(2 * 60.0 / bpm / bin_sec))
    smooth = np.convolve(env, np.ones(win, dtype=np.float32) / win, mode="same")
    ref = float(np.percentile(smooth, 95))
    if ref <= 0:
        return None
    loud = np.nonzero(smooth >= 0.8 * ref)[0]
    if loud.size == 0:
        return None
    return float(loud[-1] * bin_sec)


@router.post("/suggest")
def suggest(pair: SeamPairIn) -> SeamSuggestion:
    """Structure-heuristic suggestion (DESIGN.md #9).

    Current rules: blend when the BPM gap is <= 10% (else cut); exit on the
    last 32-beat phrase boundary where the outgoing track is still at full
    energy — the end of its last kick section, not the outro. Falls back to
    section labels / track end when peaks can't be computed. Enter at the
    start of the incoming track.
    TODO(seams): pick the incoming entry point the same way (intro vs. slam
    into drop), offer multiple ranked candidates, and honor tempo ramps.
    """
    with db.connect() as conn:
        out_a = _analysis(conn, pair.out_track_id)
        in_a = _analysis(conn, pair.in_track_id)

    bpm_gap_pct = abs(out_a["bpm"] - in_a["bpm"]) / out_a["bpm"] * 100
    template = "blend" if bpm_gap_pct <= 10.0 else "cut"

    beat = 60.0 / out_a["bpm"]
    phrase = 32 * beat
    last_loud = None
    try:
        wf = peaks.get_or_compute(pair.out_track_id, out_a["path"])
        last_loud = _last_full_energy_sec(wf.peaks, wf.bin_sec, out_a["bpm"])
    except decode.DecodeError:
        pass

    if last_loud is not None:
        # Floor to the phrase grid (with a little tolerance for kick sections
        # that end exactly on a boundary) so the exit never lands in the outro.
        n = math.floor((last_loud - out_a["beat_offset_sec"]) / phrase + 0.05)
        source = f"track at full energy until {last_loud:.1f}s"
    else:
        sections = json.loads(out_a["sections_json"])
        drops = [s for s in sections if s["label"] == "drop"]
        raw_exit = drops[-1]["end_sec"] if drops else (out_a["duration_sec"] or 0) - 15.0
        n = round((raw_exit - out_a["beat_offset_sec"]) / phrase)
        source = "end of last detected drop" if drops else "near track end, no energy/drop data"
    n = max(0, n)
    if out_a["duration_sec"]:
        n = min(n, max(0, math.floor((out_a["duration_sec"] - out_a["beat_offset_sec"]) / phrase)))
    out_point = out_a["beat_offset_sec"] + n * phrase

    params = SeamParams(template=template, out_point_sec=round(out_point, 3), in_point_sec=0.0)
    rationale = (
        f"BPM gap {bpm_gap_pct:.1f}% -> {template}; exit on 32-beat phrase boundary "
        f"at {out_point:.1f}s ({source})"
    )

    if template == "blend":
        # Classic hard-dance bass swap: keep the incoming lows killed, then
        # trade basslines over the last 8 beats of the blend window.
        b = float(params.blend_beats)
        params.in_auto.eq_low_db = [
            CurvePoint(beat=0, value=-26),
            CurvePoint(beat=max(0.0, b - 8), value=-26),
            CurvePoint(beat=b, value=0),
        ]
        params.out_auto.eq_low_db = [
            CurvePoint(beat=max(0.0, b - 8), value=0),
            CurvePoint(beat=b, value=-26),
        ]
        rationale += "; low EQ swap over the last 8 beats"

    return SeamSuggestion(params=params, rationale=rationale)


class SeamPreviewIn(BaseModel):
    out_track_id: int
    in_track_id: int
    params: SeamParams


@router.post("/preview")
def render_preview(req: SeamPreviewIn) -> SeamPreviewOut:
    """Render tempo-matched raw segments around one seam for the client's
    Web Audio engine (hybrid preview, DESIGN.md #8). Only cut points, window,
    template and tempi affect the segments — curve edits hit the cache."""
    with db.connect() as conn:
        out_a = _analysis(conn, req.out_track_id)
        in_a = _analysis(conn, req.in_track_id)
    try:
        seg = preview_audio.render_segments(out_a, in_a, req.params)
    except decode.DecodeError as e:
        raise HTTPException(500, str(e)) from e
    return SeamPreviewOut(
        key=seg.key,
        sample_rate=config.RENDER_SAMPLE_RATE,
        tau0_sec=seg.tau0_sec,
        entry_sec=seg.entry_sec,
        window_sec=seg.window_sec,
        duration_sec=seg.duration_sec,
        out_url=f"/api/seams/preview/{seg.key}/out.wav",
        in_url=f"/api/seams/preview/{seg.key}/in.wav",
    )


@router.get("/preview/{key}/{side}.wav")
def preview_wav(key: str, side: str) -> FileResponse:
    if side not in ("out", "in") or not re.fullmatch(r"[0-9a-f]{16}", key):
        raise HTTPException(404, "no such preview segment")
    path = config.CACHE_DIR / "preview" / f"{key}_{side}.wav"
    if not path.exists():
        raise HTTPException(404, "preview segment expired — request the preview again")
    return FileResponse(path, media_type="audio/wav")
