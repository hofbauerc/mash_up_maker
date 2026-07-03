"""Render engine: grid-aware blends, automation curves, sweeps, tails.

All audio is synthetic (tones / silence via soundfile), so every assertion
is about DSP the render engine itself performed. Tests that decode audio
need ffmpeg, like the preview tests.
"""

import shutil

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import config, db
from app.audio import render
from app.main import app
from app.models import CurvePoint, Project, Seam, SeamParams

needs_ffmpeg = pytest.mark.skipif(shutil.which(config.FFMPEG) is None, reason="ffmpeg not on PATH")

SR = config.RENDER_SAMPLE_RATE


@pytest.fixture(autouse=True)
def _clean_tracks():
    db.init_db()
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM tracks")  # cascades to analysis


def _tone(path, freq: float, seconds: float, amp: float = 0.5) -> None:
    t = np.arange(int(seconds * SR)) / SR
    y = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    sf.write(path, np.column_stack([y, y]), SR)


def _silence(path, seconds: float) -> None:
    sf.write(path, np.zeros((int(seconds * SR), 2), dtype=np.float32), SR)


def _row(track_id: int, path, bpm: float) -> dict:
    return {
        "id": track_id,
        "path": str(path),
        "filename": path.name,
        "bpm": bpm,
        "beat_offset_sec": 0.0,
    }


def _project(name: str, params: SeamParams) -> Project:
    return Project(
        name=name,
        track_ids=[1, 2],
        seams=[Seam(out_track_id=1, in_track_id=2, params=params)],
    )


def _mix(tmp_path, name: str) -> np.ndarray:
    data, sr = sf.read(tmp_path / "out" / f"{name}.wav")
    assert sr == SR
    return data


def _rms(mix: np.ndarray, t0: float, t1: float) -> float:
    seg = mix[int(t0 * SR) : int(t1 * SR)]
    return float(np.sqrt((seg**2).mean()))


@needs_ffmpeg
def test_blend_render_tempo_matches_and_ramps(tmp_path):
    """150 -> 160 BPM blend: the incoming window plays at the outgoing tempo,
    then ramps back to native, so segment lengths follow the grid math."""
    _tone(tmp_path / "a.wav", 220, 12.0)
    _tone(tmp_path / "b.wav", 330, 20.0)
    out_bpm, in_bpm = 150.0, 160.0
    params = SeamParams(template="blend", out_point_sec=8.0, in_point_sec=1.0, blend_beats=8)

    result = render.render_set(
        _project("blendset", params),
        [_row(1, tmp_path / "a.wav", out_bpm), _row(2, tmp_path / "b.wav", in_bpm)],
        tmp_path / "out",
    )

    factor = out_bpm / in_bpm
    window = 8 * 60.0 / out_bpm  # 3.2 s of output
    entry = 8.0 - window  # 4.8 s
    ramp_out = sum(
        (60.0 / in_bpm) / (factor + (k + 0.5) / render.RAMP_BEATS * (1 - factor))
        for k in range(render.RAMP_BEATS)
    )
    body = 20.0 - 1.0 - window * factor - render.RAMP_BEATS * 60.0 / in_bpm
    expected = entry + window + ramp_out + body
    assert abs(result.duration_sec - expected) < 0.1
    assert ramp_out > render.RAMP_BEATS * 60.0 / in_bpm + 0.2  # slowed-down ramp is longer

    tracklist = result.tracklist_path.read_text(encoding="utf-8").splitlines()
    assert tracklist[0].startswith("0:00:00") and tracklist[0].endswith("a.wav")
    assert tracklist[1].startswith("0:00:04") and tracklist[1].endswith("b.wav")


@needs_ffmpeg
def test_volume_curve_silences_outgoing(tmp_path):
    """An out-side volume curve hitting 0 at beat 4 must mute the outgoing
    track from there — curves render, not just template fades."""
    _tone(tmp_path / "a.wav", 220, 12.0)
    _silence(tmp_path / "b.wav", 12.0)
    params = SeamParams(template="blend", out_point_sec=8.0, in_point_sec=0.0, blend_beats=8)
    params.out_auto.volume = [CurvePoint(beat=0, value=1.0), CurvePoint(beat=4, value=0.0)]

    render.render_set(
        _project("volset", params),
        [_row(1, tmp_path / "a.wav", 150.0), _row(2, tmp_path / "b.wav", 150.0)],
        tmp_path / "out",
    )
    mix = _mix(tmp_path, "volset")
    # window = [4.8, 8.0]; beat 4 = 6.4 s. Before the window: untouched tone.
    assert _rms(mix, 3.0, 4.0) == pytest.approx(0.5 / np.sqrt(2), rel=0.05)
    assert _rms(mix, 6.6, 8.0) < 0.01


@needs_ffmpeg
def test_lowpass_sweep_attenuates(tmp_path):
    """A lowpass sweep down to 150 Hz must strip a 4 kHz tone by the exit."""
    _tone(tmp_path / "a.wav", 4000, 12.0)
    _silence(tmp_path / "b.wav", 12.0)
    params = SeamParams(template="cut", out_point_sec=8.0, in_point_sec=0.0, blend_beats=8)
    params.out_auto.filter.kind = "lowpass"
    params.out_auto.filter.cutoff_hz = [
        CurvePoint(beat=0, value=20000),
        CurvePoint(beat=8, value=150),
    ]

    render.render_set(
        _project("sweepset", params),
        [_row(1, tmp_path / "a.wav", 150.0), _row(2, tmp_path / "b.wav", 150.0)],
        tmp_path / "out",
    )
    mix = _mix(tmp_path, "sweepset")
    assert _rms(mix, 3.0, 4.0) > 0.25  # pre-window: sweep still at 20 kHz
    # The cutoff ramps linearly in Hz (like the preview's AudioParam), so it
    # only dives below the tone near the exit — measure the last beat.
    assert _rms(mix, 7.9, 7.99) < 0.05 * _rms(mix, 3.0, 4.0)


@needs_ffmpeg
def test_eq_low_kill_attenuates_bass(tmp_path):
    """EQ low curve to -26 dB must kill a 100 Hz tone (bass swap machinery)."""
    _tone(tmp_path / "a.wav", 100, 12.0)
    _silence(tmp_path / "b.wav", 12.0)
    params = SeamParams(template="cut", out_point_sec=8.0, in_point_sec=0.0, blend_beats=8)
    params.out_auto.eq_low_db = [
        CurvePoint(beat=0, value=0.0),
        CurvePoint(beat=4, value=-26.0),
    ]

    render.render_set(
        _project("eqset", params),
        [_row(1, tmp_path / "a.wav", 150.0), _row(2, tmp_path / "b.wav", 150.0)],
        tmp_path / "out",
    )
    mix = _mix(tmp_path, "eqset")
    assert _rms(mix, 7.5, 7.99) < 0.25 * _rms(mix, 3.0, 4.0)


@needs_ffmpeg
def test_delay_tail_rings_past_exit(tmp_path):
    """With a delay tail, the outgoing track keeps echoing after a hard cut."""
    _tone(tmp_path / "a.wav", 220, 12.0)
    _silence(tmp_path / "b.wav", 12.0)
    params = SeamParams(template="cut", out_point_sec=8.0, in_point_sec=0.0, blend_beats=8)
    params.tail.kind = "delay"
    params.tail.wet = 0.6
    params.tail.time_beats = 0.75  # 0.3 s at 150 BPM
    params.tail.feedback = 0.5

    render.render_set(
        _project("tailset", params),
        [_row(1, tmp_path / "a.wav", 150.0), _row(2, tmp_path / "b.wav", 150.0)],
        tmp_path / "out",
    )
    mix = _mix(tmp_path, "tailset")
    assert _rms(mix, 8.05, 8.6) > 0.02  # echoes of the exit under the (silent) incoming
    assert _rms(mix, 8.05, 8.6) < _rms(mix, 7.0, 8.0)  # ...but quieter than the source


@needs_ffmpeg
def test_export_endpoint_end_to_end(tmp_path):
    _tone(tmp_path / "a.wav", 220, 12.0)
    _tone(tmp_path / "b.wav", 330, 12.0)
    params = {"template": "cut", "out_point_sec": 8.0, "in_point_sec": 2.0, "blend_beats": 8}
    with TestClient(app) as client:
        with db.connect() as conn:
            for n, name in enumerate(("a.wav", "b.wav"), start=1):
                conn.execute(
                    "INSERT INTO tracks (id, path, filename, duration_sec, analysis_status)"
                    " VALUES (?, ?, ?, 12.0, 'done')",
                    (n, str(tmp_path / name), name),
                )
                conn.execute(
                    "INSERT INTO analysis (track_id, bpm, beat_offset_sec) VALUES (?, 150.0, 0.0)",
                    (n,),
                )
        project = {
            "name": "e2e",
            "track_ids": [1, 2],
            "seams": [{"out_track_id": 1, "in_track_id": 2, "params": params}],
        }
        assert client.put("/api/projects/e2e", json=project).status_code == 200
        res = client.post("/api/export/e2e")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["duration_sec"] == pytest.approx(8.0 + 10.0, abs=0.05)  # cut at 8, b from 2 to 12
    assert (config.EXPORTS_DIR / "e2e" / "e2e.wav").exists()
    tracklist = (config.EXPORTS_DIR / "e2e" / "e2e_tracklist.txt").read_text(encoding="utf-8")
    assert "0:00:08  b.wav" in tracklist


def test_export_requires_analysis(tmp_path):
    with TestClient(app) as client:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO tracks (id, path, filename, analysis_status)"
                " VALUES (1, ?, 'x.wav', 'pending')",
                (str(tmp_path / "x.wav"),),
            )
        project = {"name": "unready", "track_ids": [1], "seams": []}
        assert client.put("/api/projects/unready", json=project).status_code == 200
        res = client.post("/api/export/unready")
    assert res.status_code == 409
    assert "not analyzed" in res.json()["detail"]
