"""Background job queues: analysis and stem separation over the DB tables.

Analysis is CPU-bound but releases the GIL in numpy/librosa for long
stretches, so a couple of threads keep the API responsive enough for a
personal tool. Stem separation (Demucs) saturates every core by itself and
takes minutes per track, so it gets its own single-worker pool — one
separation at a time, never starving the analysis queue. Results and
failures land in SQLite; the frontend polls.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import db
from .audio import stems
from .audio.analyze import analyze_track

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_stems_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def start() -> None:
    global _executor, _stems_executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")
        if _stems_executor is None:
            _stems_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stems")


def shutdown() -> None:
    global _executor, _stems_executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None
        if _stems_executor is not None:
            _stems_executor.shutdown(wait=False, cancel_futures=True)
            _stems_executor = None


def enqueue_pending() -> int:
    """Submit every 'pending' track for analysis; returns how many were queued."""
    assert _executor is not None, "worker not started"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, path FROM tracks WHERE analysis_status = 'pending'"
        ).fetchall()
    for row in rows:
        _executor.submit(_run_one, row["id"], row["path"])
    return len(rows)


def _run_one(track_id: int, path: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE tracks SET analysis_status='running', analysis_error=NULL WHERE id=?",
            (track_id,),
        )
    try:
        result = analyze_track(path)
    except Exception as e:  # noqa: BLE001 — any failure must land in the DB, not kill the pool
        log.exception("analysis failed for %s", path)
        with db.connect() as conn:
            conn.execute(
                "UPDATE tracks SET analysis_status='error', analysis_error=? WHERE id=?",
                (str(e)[:500], track_id),
            )
        return
    with db.connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO analysis
               (track_id, bpm, beat_offset_sec, key_name, camelot, energy, sections_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                track_id,
                result.bpm,
                result.beat_offset_sec,
                result.key_name,
                result.camelot,
                result.energy,
                json.dumps([s.model_dump() for s in result.sections]),
            ),
        )
        conn.execute("UPDATE tracks SET analysis_status='done' WHERE id=?", (track_id,))


def enqueue_stems(track_id: int, path: str) -> None:
    assert _stems_executor is not None, "worker not started"
    _stems_executor.submit(_run_stems, track_id, path)


def resume_stems() -> int:
    """Re-queue separations interrupted by a restart; returns how many."""
    assert _stems_executor is not None, "worker not started"
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT s.track_id, t.path FROM stems s JOIN tracks t ON t.id = s.track_id
               WHERE s.status IN ('pending', 'running')"""
        ).fetchall()
    for row in rows:
        _stems_executor.submit(_run_stems, row["track_id"], row["path"])
    return len(rows)


def _run_stems(track_id: int, path: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE stems SET status='running', error=NULL WHERE track_id=?", (track_id,)
        )
    try:
        stems.separate_track(track_id, path)
    except Exception as e:  # noqa: BLE001 — any failure must land in the DB, not kill the pool
        log.exception("stem separation failed for %s", path)
        with db.connect() as conn:
            conn.execute(
                "UPDATE stems SET status='error', error=? WHERE track_id=?",
                (str(e)[:500], track_id),
            )
        return
    with db.connect() as conn:
        conn.execute("UPDATE stems SET status='done' WHERE track_id=?", (track_id,))
