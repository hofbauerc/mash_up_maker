"""Library endpoints: registered folders, scanning, track listing."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import config, db, worker
from ..audio import decode, peaks
from ..models import AnalysisOut, Section, TrackOut, WaveformOut

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


# TODO(analysis-ui): PATCH endpoint for manual grid/section correction
# (nudge beat_offset_sec, override bpm, relabel sections) — DESIGN.md #5.
