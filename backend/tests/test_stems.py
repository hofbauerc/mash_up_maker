"""Stem separation (Phase 2): cache, window math, DSP integration, endpoints.

The Demucs backend is swapped for fakes — these tests cover everything
around it (caching, the source-domain window rewrite, render/preview
integration and the job endpoints), not the neural net.
"""

import shutil
import time

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import config, db
from app.audio import preview, render, stems
from app.main import app
from app.models import Project, Seam, SeamParams, StemMix
from app.routers.projects import save_project

needs_ffmpeg = pytest.mark.skipif(shutil.which(config.FFMPEG) is None, reason="ffmpeg not on PATH")

SR = config.RENDER_SAMPLE_RATE


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    db.init_db()
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM tracks")  # cascades to analysis + stems rows
    shutil.rmtree(config.CACHE_DIR / "stems", ignore_errors=True)
    # Preview segments are keyed by track id, which tests reuse across
    # unrelated temp files — never let one test's cache serve another's.
    shutil.rmtree(config.CACHE_DIR / "preview", ignore_errors=True)


def _tone(path, freq: float, seconds: float, amp: float = 0.5) -> None:
    t = np.arange(int(seconds * SR)) / SR
    y = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    sf.write(path, np.column_stack([y, y]), SR)


def _write_fake_stems(track_id: int, audio: np.ndarray) -> None:
    """drums = the whole master, other stems silent — so muting drums must
    silence the window and any other mix is trivially predictable."""
    d = stems.stems_dir(track_id)
    d.mkdir(parents=True, exist_ok=True)
    zero = np.zeros_like(audio)
    for name in stems.STEM_NAMES:
        sf.write(d / f"{name}.wav", audio if name == "drums" else zero, SR, subtype="PCM_16")


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


def test_apply_mix_window_rewrites_only_the_window():
    n = 4 * SR
    t = np.arange(n) / SR
    audio = np.column_stack([np.sin(2 * np.pi * 220 * t)] * 2).astype(np.float32) * 0.5
    _write_fake_stems(7, audio)

    out = stems.apply_mix_window(audio, SR, 7, 1.0, 3.0, StemMix(drums=0.0))
    assert np.array_equal(out[: SR - 1], audio[: SR - 1])  # before: untouched
    assert np.array_equal(out[3 * SR + 1 :], audio[3 * SR + 1 :])  # after: untouched
    assert _rms(out, 1.1, 2.9) < 1e-3  # window: drums (the whole master) muted
    assert _rms(out, 0.0, 0.9) == pytest.approx(0.5 / np.sqrt(2), rel=0.05)

    passthrough = stems.apply_mix_window(audio, SR, 7, 1.0, 3.0, StemMix())
    # drums stem == master, so all-unity reproduces it (16-bit quantization).
    assert np.abs(passthrough - audio).max() < 2e-4


def test_apply_mix_window_requires_stems():
    audio = np.zeros((SR, 2), dtype=np.float32)
    with pytest.raises(stems.StemsMissing):
        stems.apply_mix_window(audio, SR, 999, 0.0, 1.0, StemMix(drums=0.0))


def test_in_window_span_blend_vs_cut():
    p_blend = SeamParams(template="blend", blend_beats=32)
    p_cut = SeamParams(template="cut", blend_beats=32)
    # Blend: 32 incoming beats; cut: 32 outgoing beats of wall time.
    assert stems.in_window_span_sec(p_blend, 150.0, 160.0) == pytest.approx(32 * 60 / 160)
    assert stems.in_window_span_sec(p_cut, 150.0, 160.0) == pytest.approx(32 * 60 / 150)


@needs_ffmpeg
def test_separate_track_writes_aligned_cache(tmp_path, monkeypatch):
    _tone(tmp_path / "a.wav", 220, 3.0)
    monkeypatch.setattr(
        stems, "_backend", lambda audio, sr: {n: audio * 0.25 for n in stems.STEM_NAMES}
    )
    stems.separate_track(5, str(tmp_path / "a.wav"))
    assert stems.stems_ready(5)
    from app.audio import decode

    n = len(decode.decode(str(tmp_path / "a.wav"), sample_rate=SR, mono=False))
    for name in stems.STEM_NAMES:
        data, sr = sf.read(stems.stems_dir(5) / f"{name}.wav")
        assert sr == SR and len(data) == n  # sample-aligned with our decode


@needs_ffmpeg
def test_render_kick_swap_mutes_outgoing_window(tmp_path):
    """out_stems drums=0 with drums==master must silence exactly the window."""
    _tone(tmp_path / "a.wav", 220, 12.0)
    _tone(tmp_path / "b.wav", 0.0, 12.0, amp=0.0)  # silence
    from app.audio import decode

    _write_fake_stems(1, decode.decode(str(tmp_path / "a.wav"), sample_rate=SR, mono=False))

    params = SeamParams(template="cut", out_point_sec=8.0, in_point_sec=0.0, blend_beats=8)
    params.out_stems = StemMix(drums=0.0)
    render.render_set(
        Project(name="kickset", track_ids=[1, 2], seams=[Seam(out_track_id=1, in_track_id=2, params=params)]),
        [_row(1, tmp_path / "a.wav", 150.0), _row(2, tmp_path / "b.wav", 150.0)],
        tmp_path / "out",
    )
    mix, _ = sf.read(tmp_path / "out" / "kickset.wav")
    assert _rms(mix, 3.0, 4.0) > 0.3  # body untouched
    assert _rms(mix, 5.0, 7.9) < 1e-3  # window [4.8, 8.0] muted by the stem mix


@needs_ffmpeg
def test_preview_segments_bake_stem_mix(tmp_path):
    _tone(tmp_path / "a.wav", 220, 12.0)
    _tone(tmp_path / "b.wav", 330, 12.0)
    from app.audio import decode

    _write_fake_stems(1, decode.decode(str(tmp_path / "a.wav"), sample_rate=SR, mono=False))
    out_a = {"track_id": 1, "path": str(tmp_path / "a.wav"), "duration_sec": 12.0, "bpm": 150.0}
    in_a = {"track_id": 2, "path": str(tmp_path / "b.wav"), "duration_sec": 12.0, "bpm": 150.0}

    plain = SeamParams(template="cut", out_point_sec=8.0, blend_beats=8)
    mixed = plain.model_copy(deep=True)
    mixed.out_stems = StemMix(drums=0.0)

    seg_plain = preview.render_segments(out_a, in_a, plain)
    seg_mixed = preview.render_segments(out_a, in_a, mixed)
    assert seg_plain.key != seg_mixed.key  # stem mix is part of the segment cache key

    audio_plain, _ = sf.read(seg_plain.out_path)
    audio_mixed, _ = sf.read(seg_mixed.out_path)
    # The outgoing segment ends at the exit; its last window is stem-muted
    # (measured inside the 10 ms edge crossfades back to the master).
    assert _rms(audio_mixed[-int(2 * SR) : -int(0.05 * SR)], 0.0, 1.9) < 1e-3
    assert _rms(audio_plain[-int(2 * SR) : -int(0.05 * SR)], 0.0, 1.9) > 0.3


@needs_ffmpeg
def test_preview_endpoint_409_when_stems_missing(tmp_path):
    _tone(tmp_path / "a.wav", 220, 12.0)
    _tone(tmp_path / "b.wav", 330, 12.0)
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
        params = {"template": "cut", "out_point_sec": 8.0, "blend_beats": 8,
                  "out_stems": {"drums": 0.0, "bass": 1.0, "vocals": 1.0, "other": 1.0}}
        res = client.post(
            "/api/seams/preview",
            json={"out_track_id": 1, "in_track_id": 2, "params": params},
        )
    assert res.status_code == 409
    assert "stems" in res.json()["detail"]


def test_stems_endpoints_flow(tmp_path, monkeypatch):
    """POST queues a background job; the fake separator lands 'done'."""
    def fake_separate(track_id: int, path: str) -> None:
        _write_fake_stems(track_id, np.zeros((SR, 2), dtype=np.float32))

    monkeypatch.setattr(stems, "separate_track", fake_separate)
    (tmp_path / "a.wav").write_bytes(b"unused by the fake")
    with TestClient(app) as client:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO tracks (id, path, filename, analysis_status) VALUES (1, ?, 'a.wav', 'done')",
                (str(tmp_path / "a.wav"),),
            )
        assert client.get("/api/library/tracks/1/stems").json()["status"] == "none"
        assert client.post("/api/library/tracks/1/stems").json()["status"] == "pending"

        deadline = time.time() + 5
        status = "pending"
        while time.time() < deadline and status not in ("done", "error"):
            time.sleep(0.05)
            status = client.get("/api/library/tracks/1/stems").json()["status"]
        assert status == "done"
        # Idempotent once done: a second POST doesn't re-queue.
        assert client.post("/api/library/tracks/1/stems").json()["status"] == "done"

        assert client.post("/api/library/tracks/99/stems").status_code == 404


def test_seam_params_stems_backward_compat():
    """Legacy seam JSON without stem fields loads as passthrough, and a
    passthrough mix never touches the stems cache."""
    params = SeamParams.model_validate({"template": "blend", "out_point_sec": 8.0})
    assert not params.out_stems.active and not params.in_stems.active

    roundtrip = SeamParams.model_validate_json(
        SeamParams(out_stems=StemMix(drums=0.0)).model_dump_json()
    )
    assert roundtrip.out_stems.active and roundtrip.out_stems.drums == 0.0

    project = Project(name="compat", track_ids=[1, 2], seams=[Seam(out_track_id=1, in_track_id=2)])
    save_project("compat", project)
