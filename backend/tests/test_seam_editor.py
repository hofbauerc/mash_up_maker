"""Seam editor backend: peaks, extended SeamParams, exit heuristic, preview."""

import io
import json
import shutil

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from app.models import SeamParams
from app.routers.seams import _last_full_energy_sec


@pytest.fixture(autouse=True)
def _clean_tracks():
    """The test DB is shared module-to-module; leave no tracks behind."""
    db.init_db()
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM tracks")  # cascades to analysis


def _insert_track(path: str, filename: str, duration: float = 2.0) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO tracks (path, filename, duration_sec, analysis_status) VALUES (?, ?, ?, 'done')",
            (path, filename, duration),
        )
        return cur.lastrowid


def _insert_analysis(track_id: int, bpm: float, sections_json: str = "[]") -> None:
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO analysis (track_id, bpm, beat_offset_sec, key_name, camelot, energy, sections_json)
               VALUES (?, ?, 0.0, 'A minor', '8A', 0.5, ?)""",
            (track_id, bpm, sections_json),
        )


def test_seam_params_backward_compat():
    """Pre-automation project JSON must still load, with default lanes."""
    legacy = {"template": "cut", "out_point_sec": 12.0, "in_point_sec": 0.0, "blend_beats": 16}
    p = SeamParams.model_validate(legacy)
    assert p.template == "cut"
    assert p.out_auto.volume == [] and p.in_auto.eq_low_db == []
    assert p.out_auto.filter.kind == "off"
    assert p.tail.kind == "none"


def test_seam_params_roundtrip_through_project():
    seam = {
        "out_track_id": 1,
        "in_track_id": 2,
        "params": {
            "template": "blend",
            "out_point_sec": 180.0,
            "in_point_sec": 4.0,
            "blend_beats": 32,
            "out_auto": {
                "eq_low_db": [{"beat": 24, "value": 0}, {"beat": 32, "value": -26}],
                "filter": {"kind": "highpass", "cutoff_hz": [{"beat": 0, "value": 20}, {"beat": 32, "value": 800}]},
            },
            "in_auto": {"volume": [{"beat": 0, "value": 0.5}, {"beat": 32, "value": 1.0}]},
            "tail": {"kind": "delay", "wet": 0.4, "time_beats": 0.75, "feedback": 0.5},
        },
    }
    with TestClient(app) as client:
        project = {"name": "curveset", "track_ids": [1, 2], "seams": [seam]}
        assert client.put("/api/projects/curveset", json=project).status_code == 200
        loaded = client.get("/api/projects/curveset").json()
    params = loaded["seams"][0]["params"]
    assert params["out_auto"]["eq_low_db"] == [{"beat": 24, "value": 0}, {"beat": 32, "value": -26}]
    assert params["out_auto"]["filter"]["kind"] == "highpass"
    assert params["in_auto"]["volume"][0] == {"beat": 0, "value": 0.5}
    assert params["tail"]["kind"] == "delay"


def test_suggest_seeds_bass_swap_for_blend():
    with TestClient(app) as client:
        a = _insert_track("/nowhere/a.wav", "a.wav")
        b = _insert_track("/nowhere/b.wav", "b.wav")
        _insert_analysis(a, bpm=150.0)
        _insert_analysis(b, bpm=152.0)
        res = client.post("/api/seams/suggest", json={"out_track_id": a, "in_track_id": b})
    assert res.status_code == 200
    params = res.json()["params"]
    assert params["template"] == "blend"
    lows_in = params["in_auto"]["eq_low_db"]
    lows_out = params["out_auto"]["eq_low_db"]
    assert lows_in[0]["value"] == -26 and lows_in[-1]["value"] == 0  # incoming bass comes in
    assert lows_out[0]["value"] == 0 and lows_out[-1]["value"] == -26  # outgoing bass leaves


def _seed_peaks_cache(track_id: int, loud_sec: float, total_sec: float) -> None:
    """Fake a cached peaks file: full level until loud_sec, quiet outro after.
    Includes the spectral fields — caches without them are recomputed."""
    bin_sec = 0.02
    n_loud, n_total = int(loud_sec / bin_sec), int(total_sec / bin_sec)
    peaks = [0.95] * n_loud + [0.25] * (n_total - n_loud)
    wf = {
        "track_id": track_id,
        "bin_sec": bin_sec,
        "duration_sec": total_sec,
        "peaks": peaks,
        "rms": [p * 0.5 for p in peaks],
        "bands": [[p * 0.3, p * 0.3, p * 0.1] for p in peaks],
    }
    d = config.CACHE_DIR / "peaks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{track_id}_50.json").write_text(json.dumps(wf), encoding="utf-8")


def test_last_full_energy_sec():
    bin_sec = 0.02
    peaks = [0.95] * int(160 / bin_sec) + [0.25] * int(40 / bin_sec)
    t = _last_full_energy_sec(peaks, bin_sec, bpm=150.0)
    assert abs(t - 160.0) < 1.0  # ~2-beat smoothing blurs the edge slightly
    assert _last_full_energy_sec([], bin_sec, bpm=150.0) is None
    assert _last_full_energy_sec([0.0] * 100, bin_sec, bpm=150.0) is None


def test_suggest_exits_at_last_kick_section_not_outro():
    """150 BPM, offset 0 -> phrase = 12.8s. Full energy until 160s, quiet
    outro to 200s: exit must floor to 12 * 12.8 = 153.6s, not land near 200."""
    with TestClient(app) as client:
        a = _insert_track("/nowhere/loud.wav", "loud.wav", duration=200.0)
        b = _insert_track("/nowhere/next.wav", "next.wav", duration=200.0)
        _insert_analysis(a, bpm=150.0)
        _insert_analysis(b, bpm=150.0)
        _seed_peaks_cache(a, loud_sec=160.0, total_sec=200.0)
        res = client.post("/api/seams/suggest", json={"out_track_id": a, "in_track_id": b})
    assert res.status_code == 200
    body = res.json()
    assert abs(body["params"]["out_point_sec"] - 153.6) < 0.01
    assert "full energy" in body["rationale"]


@pytest.mark.skipif(shutil.which(config.FFMPEG) is None, reason="ffmpeg not on PATH")
def test_preview_segments_endpoint(tmp_path):
    sr = 44100
    t = np.arange(12 * sr) / sr
    tone = (0.8 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    sf.write(tmp_path / "a.wav", tone, sr)
    sf.write(tmp_path / "b.wav", tone, sr)

    # 150 BPM (beat 0.4s), 8-beat window (3.2s), exit 8.0s: the 16-beat lead
    # is clamped at track start, entry = 8.0 - 3.2 = 4.8s in preview time.
    params = {"template": "blend", "out_point_sec": 8.0, "in_point_sec": 1.0, "blend_beats": 8}
    with TestClient(app) as client:
        a = _insert_track(str(tmp_path / "a.wav"), "a.wav", duration=12.0)
        b = _insert_track(str(tmp_path / "b.wav"), "b.wav", duration=12.0)
        _insert_analysis(a, bpm=150.0)
        _insert_analysis(b, bpm=150.0)
        res = client.post(
            "/api/seams/preview",
            json={"out_track_id": a, "in_track_id": b, "params": params},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["tau0_sec"] == 0.0
        assert abs(body["entry_sec"] - 4.8) < 1e-3
        # incoming: 16s wanted from 1.0s, clamped to 11s -> ends at 4.8 + 11 = 15.8
        assert abs(body["duration_sec"] - 15.8) < 0.05

        wav = client.get(body["out_url"])
        assert wav.status_code == 200
        assert wav.headers["content-type"].startswith("audio/wav")
        data, wav_sr = sf.read(io.BytesIO(wav.content))
        assert abs(len(data) / wav_sr - 8.0) < 0.02  # outgoing segment = [0, exit]
        assert client.get(body["in_url"]).status_code == 200

        again = client.post(
            "/api/seams/preview",
            json={"out_track_id": a, "in_track_id": b, "params": params},
        )
        assert again.json()["key"] == body["key"]  # cache hit

        assert client.get("/api/seams/preview/0123456789abcdef/out.wav").status_code == 404


@pytest.mark.skipif(shutil.which(config.FFMPEG) is None, reason="ffmpeg not on PATH")
def test_peaks_endpoint(tmp_path):
    sr = 44100
    t = np.arange(2 * sr) / sr
    wav = tmp_path / "tone.wav"
    sf.write(wav, (0.8 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr)

    with TestClient(app) as client:
        track_id = _insert_track(str(wav), "tone.wav")
        res = client.get(f"/api/library/tracks/{track_id}/peaks")
        assert res.status_code == 200
        body = res.json()
        assert abs(body["duration_sec"] - 2.0) < 0.05
        n_expected = round(body["duration_sec"] / body["bin_sec"])
        assert abs(len(body["peaks"]) - n_expected) <= 1
        assert all(0.0 <= p <= 1.0 for p in body["peaks"])
        assert max(body["peaks"]) > 0.5  # the 0.8 sine survives binning

        assert (config.CACHE_DIR / "peaks" / f"{track_id}_50.json").exists()
        assert client.get(f"/api/library/tracks/{track_id}/peaks").json() == body  # cache hit

        assert client.get("/api/library/tracks/999999/peaks").status_code == 404
