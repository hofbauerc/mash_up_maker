"""Set projects: JSON files under data/projects, non-destructive by design."""

import re

from fastapi import APIRouter, HTTPException

from .. import config, db, ordering
from ..models import OrderSuggestion, Project

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
