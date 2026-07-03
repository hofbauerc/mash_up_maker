"""Smoke tests: app boots, DB initializes, pure logic works.

Test data isolation (MASHUP_DATA_DIR) happens in conftest.py. Render/export
paths are covered in test_render.py with synthetic audio — TODO(tests):
generate a 150 BPM kick loop with numpy and assert the *detected* grid too.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.ordering import camelot_distance, suggest_order


def test_health_and_boot():
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/library/tracks").json() == []


def test_project_roundtrip():
    with TestClient(app) as client:
        project = {"name": "testset", "track_ids": [], "seams": []}
        assert client.put("/api/projects/testset", json=project).status_code == 200
        assert client.get("/api/projects/testset").json()["name"] == "testset"
        assert "testset" in client.get("/api/projects").json()


def test_camelot_distance():
    assert camelot_distance("8A", "8A") == 0
    assert camelot_distance("8A", "8B") == 1  # relative major/minor
    assert camelot_distance("8A", "9A") == 1  # neighbor on the wheel
    assert camelot_distance("1A", "12A") == 1  # wheel wraps around
    assert camelot_distance("8A", None) is None


def test_suggest_order_climbs_bpm():
    tracks = [
        {"id": 1, "bpm": 180.0, "camelot": "8A", "energy": 0.9},
        {"id": 2, "bpm": 150.0, "camelot": "8A", "energy": 0.7},
        {"id": 3, "bpm": 152.0, "camelot": "9A", "energy": 0.8},
    ]
    result = suggest_order(tracks)
    assert result.track_ids == [2, 3, 1]  # starts low, climbs
    assert len(result.adjacencies) == 2
