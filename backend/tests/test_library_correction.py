"""Manual grid/section correction endpoints (DESIGN.md #5)."""

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture(autouse=True)
def _clean_tracks():
    db.init_db()
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM tracks")


def _insert_analyzed(path: str = "/nowhere/t.wav", bpm: float = 150.0, offset: float = 0.3) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO tracks (path, filename, duration_sec, analysis_status)"
            " VALUES (?, 't.wav', 200.0, 'done')",
            (path,),
        )
        track_id = cur.lastrowid
        conn.execute(
            """INSERT INTO analysis (track_id, bpm, beat_offset_sec, key_name, camelot, energy,
               sections_json) VALUES (?, ?, ?, 'A minor', '8A', 0.5, '[]')""",
            (track_id, bpm, offset),
        )
    return track_id


def test_patch_bpm_and_offset():
    with TestClient(app) as client:
        tid = _insert_analyzed(bpm=150.0, offset=0.3)
        # Nudge the anchor past one period: it must fold back into [0, beat).
        res = client.patch(
            f"/api/library/tracks/{tid}/analysis", json={"beat_offset_sec": 0.5}
        )
        assert res.status_code == 200
        assert res.json()["beat_offset_sec"] == pytest.approx(0.1)  # 0.5 % 0.4

        res = client.patch(f"/api/library/tracks/{tid}/analysis", json={"bpm": 160.0})
        assert res.status_code == 200
        body = res.json()
        assert body["bpm"] == 160.0
        assert body["key_name"] == "A minor"  # untouched fields survive

        # Corrections persist for the analysis consumers (seam math, ordering).
        assert client.get(f"/api/library/tracks/{tid}/analysis").json()["bpm"] == 160.0


def test_patch_sections_sorted_and_validated():
    with TestClient(app) as client:
        tid = _insert_analyzed()
        sections = [
            {"label": "drop", "start_sec": 60.0, "end_sec": 120.0},
            {"label": "intro", "start_sec": 0.0, "end_sec": 60.0},
        ]
        res = client.patch(f"/api/library/tracks/{tid}/analysis", json={"sections": sections})
        assert res.status_code == 200
        assert [s["label"] for s in res.json()["sections"]] == ["intro", "drop"]

        bad = [{"label": "drop", "start_sec": 10.0, "end_sec": 10.0}]
        assert (
            client.patch(f"/api/library/tracks/{tid}/analysis", json={"sections": bad}).status_code
            == 422
        )
        assert client.patch(f"/api/library/tracks/{tid}/analysis", json={"bpm": 20.0}).status_code == 422


def test_patch_unanalyzed_404():
    with TestClient(app) as client:
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO tracks (path, filename, analysis_status) VALUES ('/n/a.wav', 'a.wav', 'pending')"
            )
        res = client.patch(f"/api/library/tracks/{cur.lastrowid}/analysis", json={"bpm": 150.0})
        assert res.status_code == 404


def test_audio_endpoint_serves_file(tmp_path):
    wav = tmp_path / "tone.wav"
    sf.write(wav, np.zeros((1000, 2), dtype=np.float32), 44100)
    with TestClient(app) as client:
        tid = _insert_analyzed(path=str(wav))
        res = client.get(f"/api/library/tracks/{tid}/audio")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("audio/")
        assert len(res.content) > 0

        missing = _insert_analyzed(path=str(tmp_path / "gone.wav"))
        assert client.get(f"/api/library/tracks/{missing}/audio").status_code == 404
        assert client.get("/api/library/tracks/999999/audio").status_code == 404
