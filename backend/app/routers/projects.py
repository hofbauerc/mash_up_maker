"""Set projects: JSON files under data/projects, non-destructive by design."""

import re
import statistics

from fastapi import APIRouter, HTTPException

from .. import config, db, ordering
from ..audio import decode, peaks
from ..models import OrderSuggestion, Project, TrackGainOut

router = APIRouter(prefix="/api/projects", tags=["projects"])

_NAME_RE = re.compile(r"^[\w][\w \-]{0,80}$")


def _path(name: str):
    if not _NAME_RE.match(name):
        raise HTTPException(400, "invalid project name")
    return config.PROJECTS_DIR / f"{name}.json"


@router.get("")
def list_projects() -> list[str]:
    return sorted(p.stem for p in config.PROJECTS_DIR.glob("*.json"))


@router.get("/{name}")
def load_project(name: str) -> Project:
    p = _path(name)
    if not p.exists():
        raise HTTPException(404, f"no project named {name}")
    return Project.model_validate_json(p.read_text(encoding="utf-8"))


@router.put("/{name}")
def save_project(name: str, project: Project) -> Project:
    project.name = name
    _path(name).write_text(project.model_dump_json(indent=2), encoding="utf-8")
    return project


@router.post("/{name}/auto-gain")
def auto_gain(name: str) -> list[TrackGainOut]:
    """Suggest a per-track dB trim toward the set's median loudness, so no
    blend feels like a level dip. Suggestions only — the frontend writes
    them into the project's editable track_gains."""
    project = load_project(name)
    if not project.track_ids:
        return []
    with db.connect() as conn:
        rows = {
            r["id"]: r["path"]
            for r in conn.execute(
                f"SELECT id, path FROM tracks WHERE id IN ({','.join('?' * len(project.track_ids))})",
                project.track_ids,
            )
        }
    louds: dict[int, float | None] = {}
    for tid in project.track_ids:
        if tid not in rows:
            louds[tid] = None
            continue
        try:
            louds[tid] = peaks.loudness_db(peaks.get_or_compute(tid, rows[tid]))
        except decode.DecodeError:
            louds[tid] = None
    known = [v for v in louds.values() if v is not None]
    if not known:
        raise HTTPException(409, "no track loudness available (tracks not decodable?)")
    target = statistics.median(known)
    return [
        TrackGainOut(
            track_id=tid,
            loudness_db=round(loud, 2) if loud is not None else None,
            gain_db=round(max(-12.0, min(12.0, target - loud)), 1) if loud is not None else 0.0,
        )
        for tid, loud in louds.items()
    ]


@router.post("/{name}/suggest-order")
def suggest_order(name: str) -> OrderSuggestion:
    """Suggest an order for the project's tracks (analyzed tracks only)."""
    project = load_project(name)
    if not project.track_ids:
        return OrderSuggestion(track_ids=[], adjacencies=[])
    with db.connect() as conn:
        placeholders = ",".join("?" * len(project.track_ids))
        rows = conn.execute(
            f"""SELECT t.id, a.bpm, a.camelot, a.energy
                FROM tracks t JOIN analysis a ON a.track_id = t.id
                WHERE t.id IN ({placeholders})""",
            project.track_ids,
        ).fetchall()
    if len(rows) < len(project.track_ids):
        missing = set(project.track_ids) - {r["id"] for r in rows}
        raise HTTPException(409, f"tracks not analyzed yet: {sorted(missing)}")
    return ordering.suggest_order([dict(r) for r in rows])
