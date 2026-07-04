"""Spectral bands, loudness matching and content-aware auto-EQ."""

import shutil

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import config, db
from app.audio import autoeq, peaks, render
from app.main import app
from app.models import Project, Seam, SeamParams, WaveformOut

needs_ffmpeg = pytest.mark.skipif(shutil.which(config.FFMPEG) is None, reason="ffmpeg not on PATH")

SR = config.RENDER_SAMPLE_RATE


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM tracks")
    shutil.rmtree(config.CACHE_DIR / "peaks", ignore_errors=True)


def _tone(path, freq: float, seconds: float, amp: float = 0.5) -> None:
    t = np.arange(int(seconds * SR)) / SR
    y = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    sf.write(path, np.column_stack([y, y]), SR)


def _wave(bands: list[list[float]], bin_sec: float = 0.1) -> WaveformOut:
    n = len(bands)
    return WaveformOut(
        track_id=1,
        bin_sec=bin_sec,
        duration_sec=n * bin_sec,
        peaks=[min(sum(b), 1.0) for b in bands],
        rms=[sum(b) / 2 for b in bands],
        bands=bands,
    )


@needs_ffmpeg
def test_bands_track_the_spectrum(tmp_path):
    _tone(tmp_path / "low.wav", 80, 3.0)
    _tone(tmp_path / "high.wav", 8000, 3.0)
    lo = peaks.get_or_compute(1, str(tmp_path / "low.wav"))
    hi = peaks.get_or_compute(2, str(tmp_path / "high.wav"))
    lo_mid = np.asarray(lo.bands).mean(axis=0)
    hi_mid = np.asarray(hi.bands).mean(axis=0)
    assert lo_mid[0] > 5 * lo_mid[2]  # 80 Hz lands in the low band
    assert hi_mid[2] > 5 * hi_mid[0]  # 8 kHz lands in the high band
    # Loudness on the high tone: many cycles per bin, so bin RMS is stable
    # (an 80 Hz tone only fits ~1.6 cycles per 20 ms bin and fluctuates).
    # ffmpeg's stereo->mono downmix is power-preserving (amp x sqrt(2)), so a
    # 0.5-amp stereo sine measures RMS 0.5 — absolute convention is fine, gain
    # matching only ever uses loudness *differences*.
    assert hi.rms is not None
    assert peaks.loudness_db(hi) == pytest.approx(20 * np.log10(0.5), abs=0.5)


@needs_ffmpeg
def test_legacy_peaks_cache_recomputed(tmp_path):
    """Caches from before the spectral fields must be recomputed, not served."""
    _tone(tmp_path / "a.wav", 440, 2.0)
    first = peaks.get_or_compute(1, str(tmp_path / "a.wav"))
    cache = config.CACHE_DIR / "peaks" / "1_50.json"
    legacy = WaveformOut(track_id=1, bin_sec=first.bin_sec, duration_sec=first.duration_sec, peaks=first.peaks)
    cache.write_text(legacy.model_dump_json(), encoding="utf-8")
    again = peaks.get_or_compute(1, str(tmp_path / "a.wav"))
    assert again.bands is not None and again.rms is not None


def test_loudness_db_uses_loud_sections():
    quiet_head = [[0.0, 0.001, 0.0]] * 50 + [[0.2, 0.2, 0.1]] * 200
    loud = peaks.loudness_db(_wave(quiet_head))
    assert loud == pytest.approx(20 * np.log10(0.25), abs=0.5)  # rms = sum/2 = 0.25
    assert peaks.loudness_db(WaveformOut(track_id=1, bin_sec=0.1, duration_sec=1, peaks=[])) is None


@needs_ffmpeg
def test_auto_gain_endpoint_matches_toward_median(tmp_path):
    _tone(tmp_path / "loud.wav", 440, 3.0, amp=0.8)
    _tone(tmp_path / "quiet.wav", 440, 3.0, amp=0.2)  # 12 dB apart
    with TestClient(app) as client:
        with db.connect() as conn:
            for n, name in enumerate(("loud.wav", "quiet.wav"), start=1):
                conn.execute(
                    "INSERT INTO tracks (id, path, filename, duration_sec, analysis_status)"
                    " VALUES (?, ?, ?, 3.0, 'done')",
                    (n, str(tmp_path / name), name),
                )
        project = {"name": "gains", "track_ids": [1, 2], "seams": []}
        assert client.put("/api/projects/gains", json=project).status_code == 200
        res = client.post("/api/projects/gains/auto-gain")
    assert res.status_code == 200, res.text
    gains = {g["track_id"]: g["gain_db"] for g in res.json()}
    assert gains[1] == pytest.approx(-6.0, abs=0.3)  # both pulled to the median
    assert gains[2] == pytest.approx(+6.0, abs=0.3)


@needs_ffmpeg
def test_render_applies_track_gains(tmp_path):
    _tone(tmp_path / "a.wav", 220, 6.0)
    project = Project(name="trimset", track_ids=[1], track_gains={1: -6.0})
    render.render_set(
        project,
        [{"id": 1, "path": str(tmp_path / "a.wav"), "filename": "a.wav", "bpm": 150.0, "beat_offset_sec": 0.0}],
        tmp_path / "out",
    )
    mix, _ = sf.read(tmp_path / "out" / "trimset.wav")
    seg = mix[SR : 5 * SR]
    assert np.sqrt((seg**2).mean()) == pytest.approx(0.5 / np.sqrt(2) / 2, rel=0.05)  # −6 dB


def test_project_track_gains_backward_compat():
    p = Project.model_validate({"name": "old", "track_ids": [1], "seams": []})
    assert p.track_gains == {}


def _blend_params(bb: int = 32) -> SeamParams:
    return SeamParams(template="blend", out_point_sec=80.0, in_point_sec=0.0, blend_beats=bb)


def test_autoeq_places_swap_at_incoming_kick():
    """Incoming kick starts at window beat 16 (150 BPM, bins of 0.1 s):
    the bass swap must land there, not at a fixed offset."""
    kick_start_bin = 64  # beat 16 * 0.4 s/beat / 0.1 s/bin
    in_bands = [[0.0, 0.2, 0.1]] * kick_start_bin + [[0.4, 0.2, 0.1]] * 936
    out_bands = [[0.4, 0.02, 0.1]] * 1000  # bass throughout, no mids
    seed = autoeq.seed_eq(_wave(out_bands), _wave(in_bands), 150.0, 150.0, _blend_params())
    assert seed.in_low[0].value == -26 and seed.in_low[-1].beat == 16 and seed.in_low[-1].value == 0
    assert seed.out_low[-1].beat == 16 and seed.out_low[-1].value == -26
    assert "beat 16" in seed.rationale


def test_autoeq_skips_swap_for_kickless_incoming_window():
    """The incoming window has no bass at all (its kick lives later in the
    track) — no pointless bass-kill curves."""
    in_bands = [[0.0, 0.2, 0.1]] * 200 + [[0.4, 0.2, 0.1]] * 800  # kick after 20 s
    out_bands = [[0.4, 0.02, 0.1]] * 1000
    seed = autoeq.seed_eq(_wave(out_bands), _wave(in_bands), 150.0, 150.0, _blend_params())
    assert seed.in_low == [] and seed.out_low == []
    assert "no kick" in seed.rationale


def test_autoeq_dips_mids_only_when_both_sides_play_them():
    clash_in = [[0.4, 0.3, 0.1]] * 1000
    clash_out = [[0.4, 0.3, 0.1]] * 1000
    seed = autoeq.seed_eq(_wave(clash_out), _wave(clash_in), 150.0, 150.0, _blend_params())
    assert seed.out_mid and seed.out_mid[-1].value in (-7.0, 0.0)

    # Outgoing has mids elsewhere but its window region (67.2–80 s at 0.1 s
    # bins) is a mid-free outro — nothing clashes, nothing gets dipped.
    quiet_out = [[0.4, 0.3, 0.1]] * 672 + [[0.4, 0.01, 0.1]] * 328
    seed2 = autoeq.seed_eq(_wave(quiet_out), _wave(clash_in), 150.0, 150.0, _blend_params())
    assert seed2.out_mid == []


def test_autoeq_cut_template_stays_flat():
    seed = autoeq.seed_eq(
        _wave([[0.4, 0.2, 0.1]] * 100),
        _wave([[0.4, 0.2, 0.1]] * 100),
        150.0,
        150.0,
        SeamParams(template="cut", out_point_sec=8.0),
    )
    assert seed.out_low == [] and seed.in_low == [] and seed.out_mid == []


def test_autoeq_endpoint_returns_editable_curves(tmp_path):
    """End-to-end over HTTP with seeded spectral caches (no ffmpeg needed)."""
    with TestClient(app) as client:
        with db.connect() as conn:
            for n in (1, 2):
                conn.execute(
                    "INSERT INTO tracks (id, path, filename, duration_sec, analysis_status)"
                    " VALUES (?, ?, 'x.wav', 100.0, 'done')",
                    (n, f"/nowhere/x{n}.wav"),
                )
                conn.execute(
                    "INSERT INTO analysis (track_id, bpm, beat_offset_sec) VALUES (?, 150.0, 0.0)",
                    (n,),
                )
        d = config.CACHE_DIR / "peaks"
        d.mkdir(parents=True, exist_ok=True)
        for n in (1, 2):
            d.joinpath(f"{n}_50.json").write_text(
                _wave([[0.4, 0.2, 0.1]] * 1000).model_copy(update={"track_id": n}).model_dump_json(),
                encoding="utf-8",
            )
        res = client.post(
            "/api/seams/auto-eq",
            json={"out_track_id": 1, "in_track_id": 2, "params": _blend_params().model_dump()},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rationale"]
    assert isinstance(body["out_auto"]["eq_low_db"], list)
    assert body["in_auto"]["eq_low_db"][0]["value"] == -26  # bass held back, then swapped
