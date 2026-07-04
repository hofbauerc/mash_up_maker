"""Sample pack (Phase 1.5): synthesis, endpoint, render mixing, compat."""

import shutil

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import config, db
from app.audio import render, samples
from app.main import app
from app.models import Project, SamplePlacement, Seam, SeamParams

needs_ffmpeg = pytest.mark.skipif(shutil.which(config.FFMPEG) is None, reason="ffmpeg not on PATH")

SR = config.RENDER_SAMPLE_RATE


@pytest.fixture(autouse=True)
def _clean_tracks():
    db.init_db()
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM tracks")  # cascades to analysis


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


def _rms(mix: np.ndarray, t0: float, t1: float) -> float:
    seg = mix[int(t0 * SR) : int(t1 * SR)]
    return float(np.sqrt((seg**2).mean()))


def test_synthesis_shape_and_determinism():
    for kind in samples.KINDS:
        a = samples.synthesize(kind, 150.0, 16.0)
        b = samples.synthesize(kind, 150.0, 16.0)
        assert a.ndim == 2 and a.shape[1] == 2
        assert a.dtype == np.float32
        assert float(np.abs(a).max()) == pytest.approx(1.0, abs=1e-4)
        assert np.array_equal(a, b)  # both DSP paths must hear the same audio


def test_beat_synced_kinds_span_exact_beats():
    for kind in ("riser", "noise"):
        buf = samples.synthesize(kind, 150.0, 16.0)
        assert len(buf) == round(16 * 60.0 / 150.0 * SR)
        assert len(samples.synthesize(kind, 150.0, 8.0)) == round(8 * 60.0 / 150.0 * SR)


def test_one_shots_ignore_tempo():
    assert np.array_equal(
        samples.synthesize("impact", 150.0, 16.0), samples.synthesize("impact", 180.0, 32.0)
    )


def test_samples_endpoint_lists_and_serves():
    with TestClient(app) as client:
        kinds = client.get("/api/samples").json()
        assert {k["kind"] for k in kinds} == {"riser", "noise", "impact", "crash"}

        res = client.get("/api/samples/riser.wav?bpm=150&beats=8")
        assert res.status_code == 200
        assert res.headers["content-type"] == "audio/wav"
        assert (config.CACHE_DIR / "samples" / "riser_150.00bpm_8.wav").exists()

        assert client.get("/api/samples/nope.wav").status_code == 404


@needs_ffmpeg
def test_render_places_impact_on_the_beat(tmp_path):
    """An impact at beat=blend_beats must land exactly on the exit point;
    silence everywhere else proves only the sample was mixed in."""
    _silence(tmp_path / "a.wav", 12.0)
    _silence(tmp_path / "b.wav", 12.0)
    params = SeamParams(template="cut", out_point_sec=8.0, in_point_sec=0.0, blend_beats=8)
    params.samples = [SamplePlacement(kind="impact", beat=8, gain_db=0.0)]

    render.render_set(
        Project(name="impactset", track_ids=[1, 2], seams=[Seam(out_track_id=1, in_track_id=2, params=params)]),
        [_row(1, tmp_path / "a.wav", 150.0), _row(2, tmp_path / "b.wav", 150.0)],
        tmp_path / "out",
    )
    mix, sr = sf.read(tmp_path / "out" / "impactset.wav")
    assert sr == SR
    # Window = [4.8, 8.0] at 150 BPM; beat 8 of the window = the exit at 8.0 s.
    assert _rms(mix, 8.0, 8.3) > 0.05
    assert _rms(mix, 3.0, 7.9) < 1e-4  # nothing before the hit
    assert _rms(mix, 11.0, 12.0) < 1e-4  # decayed well before the end


@needs_ffmpeg
def test_render_riser_swells_into_the_exit(tmp_path):
    """A riser spanning the window must be present and rising toward the drop."""
    _silence(tmp_path / "a.wav", 12.0)
    _silence(tmp_path / "b.wav", 12.0)
    params = SeamParams(template="cut", out_point_sec=8.0, in_point_sec=0.0, blend_beats=8)
    params.samples = [SamplePlacement(kind="riser", beat=0, beats=8, gain_db=0.0)]

    render.render_set(
        Project(name="riserset", track_ids=[1, 2], seams=[Seam(out_track_id=1, in_track_id=2, params=params)]),
        [_row(1, tmp_path / "a.wav", 150.0), _row(2, tmp_path / "b.wav", 150.0)],
        tmp_path / "out",
    )
    mix, _ = sf.read(tmp_path / "out" / "riserset.wav")
    # Riser spans [4.8, 8.0]: silent before, quiet early, loud at the end.
    assert _rms(mix, 3.0, 4.7) < 1e-4
    assert _rms(mix, 7.5, 7.95) > 3 * _rms(mix, 5.0, 5.5)
    assert _rms(mix, 8.1, 9.0) < 1e-4  # ends at the exit


def test_seam_params_samples_backward_compat():
    """Legacy seam JSON without the samples field must still load (and old
    projects render with no samples)."""
    params = SeamParams.model_validate({"template": "cut", "out_point_sec": 8.0})
    assert params.samples == []

    roundtrip = SeamParams.model_validate_json(
        SeamParams(samples=[SamplePlacement(kind="crash", beat=32.0)]).model_dump_json()
    )
    assert roundtrip.samples[0].kind == "crash"
    assert roundtrip.samples[0].gain_db == -6.0


def test_render_skips_unknown_sample_kind(tmp_path):
    """A project from a newer version must not crash the export."""
    work = np.zeros((SR, 2), dtype=np.float32)
    params = SeamParams(samples=[SamplePlacement(kind="laser", beat=0)])
    out = render._mix_seam_samples(work, 0, 0, SR, params, 150.0)
    assert np.array_equal(out, work)
