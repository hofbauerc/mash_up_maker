"""Seam endpoints: transition suggestion + preview segments."""

import json
import math
import re

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config, db
from ..audio import autoeq, decode, peaks, stems
from ..audio import preview as preview_audio
from ..models import AutoEQOut, SeamParams, SeamPreviewOut, SeamSuggestion

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
    incoming track's first grid beat, so blends land kick-on-kick.
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
    wf = None
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

    # Enter on the incoming grid's beat 0 — beat-aligned entry is what makes
    # the tempo-matched blend land kick-on-kick (the UI snaps later edits).
    params = SeamParams(
        template=template,
        out_point_sec=round(out_point, 3),
        in_point_sec=round(in_a["beat_offset_sec"], 3),
    )
    rationale = (
        f"BPM gap {bpm_gap_pct:.1f}% -> {template}; exit on 32-beat phrase boundary "
        f"at {out_point:.1f}s ({source})"
    )

    if template == "blend":
        # Content-aware EQ seed: bass swap where the incoming kick actually
        # starts, mid dip only where the melodies collide (autoeq.py).
        try:
            in_wf = peaks.get_or_compute(pair.in_track_id, in_a["path"])
            out_wf = wf or peaks.get_or_compute(pair.out_track_id, out_a["path"])
            seed = autoeq.seed_eq(out_wf, in_wf, out_a["bpm"], in_a["bpm"], params)
        except decode.DecodeError:
            seed = autoeq._static_swap(params, "waveform unavailable — classic late bass swap")
        params = autoeq.apply_seed(params, seed)
        rationale += "; " + seed.rationale

    return SeamSuggestion(params=params, rationale=rationale)


class SeamPreviewIn(BaseModel):
    out_track_id: int
    in_track_id: int
    params: SeamParams


@router.post("/auto-eq")
def auto_eq(req: SeamPreviewIn) -> AutoEQOut:
    """Content-aware EQ seed for the seam's *current* geometry — hit it any
    time after moving the points; the result is ordinary editable curves.
    Volume and filter lanes are passed through untouched."""
    with db.connect() as conn:
        out_a = _analysis(conn, req.out_track_id)
        in_a = _analysis(conn, req.in_track_id)
    try:
        out_wf = peaks.get_or_compute(req.out_track_id, out_a["path"])
        in_wf = peaks.get_or_compute(req.in_track_id, in_a["path"])
    except decode.DecodeError as e:
        raise HTTPException(500, str(e)) from e
    seed = autoeq.seed_eq(out_wf, in_wf, out_a["bpm"], in_a["bpm"], req.params)
    merged = autoeq.apply_seed(req.params, seed)
    return AutoEQOut(out_auto=merged.out_auto, in_auto=merged.in_auto, rationale=seed.rationale)


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
    except stems.StemsMissing as e:
        raise HTTPException(409, str(e)) from e
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
