"""Library endpoints: registered folders, scanning, track listing,
manual grid/section correction (DESIGN.md #5)."""

import json
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import config, db, worker
from ..audio import decode, peaks, stems
from ..models import AnalysisOut, Section, StemsOut, TrackOut, WaveformOut

router = APIRouter(prefix="/api/library", tags=["library"])


class FolderIn(BaseModel):
    path: str


@router.get("/folders")
def list_folders() -> list[str]:
    with db.connect() as conn:
        return [r["path"] for r in conn.execute("SELECT path FROM folders ORDER BY path")]


@router.post("/folders")
def add_folder(folder: FolderIn) -> list[str]:
    p = Path(folder.path)
    if not p.is_dir():
        raise HTTPException(400, f"not a directory: {folder.path}")
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO folders (path) VALUES (?)", (str(p.resolve()),))
    return list_folders()


class ScanResult(BaseModel):
    new_tracks: int
    queued: int


@router.post("/scan")
def scan() -> ScanResult:
    """Walk all registered folders, register new audio files, queue analysis."""
    new = 0
    with db.connect() as conn:
        folders = [r["path"] for r in conn.execute("SELECT path FROM folders")]
        for folder in folders:
            for f in sorted(Path(folder).rglob("*")):
                if f.suffix.lower() not in config.AUDIO_EXTENSIONS or not f.is_file():
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tracks (path, filename, duration_sec) VALUES (?, ?, ?)",
                    (str(f), f.name, decode.probe_duration_sec(str(f))),
                )
                new += cur.rowcount
    queued = worker.enqueue_pending()
    return ScanResult(new_tracks=new, queued=queued)


@router.get("/tracks")
def list_tracks() -> list[TrackOut]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT t.*, a.bpm, a.key_name, a.camelot, a.energy
               FROM tracks t LEFT JOIN analysis a ON a.track_id = t.id
               ORDER BY t.filename"""
        ).fetchall()
    return [TrackOut(**{k: row[k] for k in TrackOut.model_fields if k in row.keys()}) for row in rows]


@router.get("/tracks/{track_id}/analysis")
def get_analysis(track_id: int) -> AnalysisOut:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM analysis WHERE track_id=?", (track_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "track not analyzed (yet)")
    return AnalysisOut(
        track_id=track_id,
        bpm=row["bpm"],
        beat_offset_sec=row["beat_offset_sec"],
        key_name=row["key_name"],
        camelot=row["camelot"],
        energy=row["energy"],
        sections=[Section(**s) for s in json.loads(row["sections_json"])],
    )


@router.get("/tracks/{track_id}/peaks")
def get_peaks(track_id: int, pps: int = Query(50, ge=10, le=200)) -> WaveformOut:
    """Waveform peaks for the seam editor, ~`pps` bins per second."""
    with db.connect() as conn:
        row = conn.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such track")
    try:
        return peaks.get_or_compute(track_id, row["path"], pps)
    except decode.DecodeError as e:
        raise HTTPException(500, str(e)) from e


@router.get("/tracks/{track_id}/audio")
def get_audio(track_id: int) -> FileResponse:
    """The original audio file — the track inspector plays it (with a
    metronome overlaid on the grid) to verify corrections by ear."""
    with db.connect() as conn:
        row = conn.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if row is None or not Path(row["path"]).is_file():
        raise HTTPException(404, "no such track")
    media_type = mimetypes.guess_type(row["path"])[0] or "application/octet-stream"
    return FileResponse(row["path"], media_type=media_type)


@router.get("/tracks/{track_id}/stems")
def get_stems(track_id: int) -> StemsOut:
    """Separation state (Phase 2). 'done' is only reported while the cached
    stem WAVs actually exist, so a cleared cache offers re-separation."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM stems WHERE track_id=?", (track_id,)
        ).fetchone()
    if row is None or (row["status"] == "done" and not stems.stems_ready(track_id)):
        return StemsOut(track_id=track_id, status="none")
    return StemsOut(track_id=track_id, status=row["status"], error=row["error"])


@router.post("/tracks/{track_id}/stems")
def request_stems(track_id: int) -> StemsOut:
    """Queue background stem separation; idempotent while one is in flight."""
    with db.connect() as conn:
        track = conn.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
        if track is None:
            raise HTTPException(404, "no such track")
        existing = conn.execute(
            "SELECT status FROM stems WHERE track_id=?", (track_id,)
        ).fetchone()
        if existing is not None and existing["status"] in ("pending", "running"):
            return StemsOut(track_id=track_id, status=existing["status"])
        if (
            existing is not None
            and existing["status"] == "done"
            and stems.stems_ready(track_id)
        ):
            return StemsOut(track_id=track_id, status="done")
        conn.execute(
            "INSERT OR REPLACE INTO stems (track_id, status, error) VALUES (?, 'pending', NULL)",
            (track_id,),
        )
    worker.enqueue_stems(track_id, track["path"])
    return StemsOut(track_id=track_id, status="pending")


class AnalysisPatch(BaseModel):
    """Manual correction: any subset of the grid + sections (DESIGN.md #5)."""

    bpm: float | None = Field(None, ge=60.0, le=300.0)
    beat_offset_sec: float | None = None
    sections: list[Section] | None = None


@router.patch("/tracks/{track_id}/analysis")
def patch_analysis(track_id: int, patch: AnalysisPatch) -> AnalysisOut:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM analysis WHERE track_id=?", (track_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "track not analyzed (yet) — nothing to correct")

        bpm = patch.bpm if patch.bpm is not None else row["bpm"]
        offset = (
            patch.beat_offset_sec if patch.beat_offset_sec is not None else row["beat_offset_sec"]
        )
        # The grid model is periodic: any nudge folds back into [0, one beat).
        offset = round(offset % (60.0 / bpm), 4)
        if patch.sections is not None:
            bad = [s for s in patch.sections if s.end_sec <= s.start_sec]
            if bad:
                raise HTTPException(422, "section end must be after its start")
            sections_json = json.dumps(
                [s.model_dump() for s in sorted(patch.sections, key=lambda s: s.start_sec)]
            )
        else:
            sections_json = row["sections_json"]

        conn.execute(
            """UPDATE analysis SET bpm=?, beat_offset_sec=?, sections_json=?,
               analyzed_at=datetime('now') WHERE track_id=?""",
            (bpm, offset, sections_json, track_id),
        )
    return AnalysisOut(
        track_id=track_id,
        bpm=bpm,
        beat_offset_sec=offset,
        key_name=row["key_name"],
        camelot=row["camelot"],
        energy=row["energy"],
        sections=[Section(**s) for s in json.loads(sections_json)],
    )
