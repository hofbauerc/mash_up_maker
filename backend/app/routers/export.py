"""Export: render a project to WAV + MP3 + tracklist under data/exports."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, db
from ..audio import render
from .projects import load_project

router = APIRouter(prefix="/api/export", tags=["export"])


class ExportResult(BaseModel):
    wav_path: str
    mp3_path: str | None
    tracklist_path: str
    duration_sec: float


@router.post("/{name}")
def export_project(name: str) -> ExportResult:
    """Synchronous for now. TODO(export): background job + progress reporting —
    a full set render takes noticeable time once tempo-matching and FX land."""
    project = load_project(name)
    if not project.track_ids:
        raise HTTPException(400, "project has no tracks")
    with db.connect() as conn:
        rows = {
            r["id"]: dict(r)
            for r in conn.execute(
                f"""SELECT id, path, filename FROM tracks
                    WHERE id IN ({",".join("?" * len(project.track_ids))})""",
                project.track_ids,
            )
        }
    try:
        track_rows = [rows[tid] for tid in project.track_ids]
    except KeyError as e:
        raise HTTPException(409, f"unknown track id in project: {e}") from e

    result = render.render_set(project, track_rows, config.EXPORTS_DIR / name)
    return ExportResult(
        wav_path=str(result.wav_path),
        mp3_path=str(result.mp3_path) if result.mp3_path else None,
        tracklist_path=str(result.tracklist_path),
        duration_sec=round(result.duration_sec, 1),
    )
