"""Stem separation (Phase 2): Demucs 4-stem split, cached per track.

Separation costs minutes per track on CPU, so it runs as a background job
(worker.py) and lands in data/cache/stems/{track_id}/ as 16-bit WAVs at the
render rate. The separation backend is pluggable — tests swap in a fake —
and the real one imports Demucs lazily because importing torch takes
seconds. Demucs is fed our own ffmpeg-decoded audio (not the file) so every
stem is sample-aligned with what the rest of the engine decodes.

The seam engine consumes stems through apply_mix_window(): a source-domain
rewrite of the transition window as a weighted stem sum, done *before* any
tempo-stretch or automation, so the render and the preview inherit identical
stem math from their shared source audio (DESIGN.md #8, risk #1).
"""

from pathlib import Path

import numpy as np
import soundfile as sf

from .. import config
from ..models import StemMix
from . import decode

STEM_NAMES = ("drums", "bass", "vocals", "other")
_EDGE_FADE_SEC = 0.010  # stems don't sum exactly to the master; hide the joins


class StemsMissing(RuntimeError):
    """A seam wants a stem mix but the track's stems aren't separated yet."""


def stems_dir(track_id: int) -> Path:
    return config.CACHE_DIR / "stems" / str(track_id)


def stems_ready(track_id: int) -> bool:
    d = stems_dir(track_id)
    return all((d / f"{name}.wav").exists() for name in STEM_NAMES)


def separate_track(track_id: int, path: str) -> None:
    """Decode at render rate, split into 4 stems, write the per-track cache."""
    audio = decode.decode(path, sample_rate=config.RENDER_SAMPLE_RATE, mono=False)
    separated = _backend(audio, config.RENDER_SAMPLE_RATE)
    d = stems_dir(track_id)
    d.mkdir(parents=True, exist_ok=True)
    for name in STEM_NAMES:
        tmp = d / f"{name}.tmp.wav"  # atomic, like the other caches
        sf.write(tmp, separated[name], config.RENDER_SAMPLE_RATE, subtype="PCM_16")
        tmp.replace(d / f"{name}.wav")


def apply_mix_window(
    audio: np.ndarray,
    sr: int,
    track_id: int,
    start_sec: float,
    end_sec: float,
    mix: StemMix,
) -> np.ndarray:
    """Rewrite [start_sec, end_sec) of `audio` as the stem-weighted sum.

    Short edge crossfades hide the residual between the stem sum and the
    original master. Returns a copy; `audio` is untouched. Raises
    StemsMissing when the track has no separated stems.
    """
    if not stems_ready(track_id):
        raise StemsMissing(
            f"track {track_id} needs its stems separated before this seam can use a stem mix"
        )
    i0 = max(int(round(start_sec * sr)), 0)
    i1 = min(int(round(end_sec * sr)), len(audio))
    if i1 <= i0:
        return audio

    d = stems_dir(track_id)
    region = np.zeros((i1 - i0, 2), dtype=np.float32)
    for name in STEM_NAMES:
        gain = getattr(mix, name)
        if gain == 0.0:
            continue
        stem, _ = sf.read(d / f"{name}.wav", start=i0, stop=i1, dtype="float32", always_2d=True)
        region[: len(stem)] += gain * stem

    out = audio.copy()
    fade = min(max(1, int(_EDGE_FADE_SEC * sr)), (i1 - i0) // 2)
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, np.newaxis]
    region[:fade] = region[:fade] * ramp + out[i0 : i0 + fade] * (1.0 - ramp)
    region[-fade:] = region[-fade:] * ramp[::-1] + out[i1 - fade : i1] * (1.0 - ramp[::-1])
    out[i0:i1] = region
    return out


def in_window_span_sec(params, out_bpm: float, in_bpm: float) -> float:
    """Incoming-track source seconds the transition window consumes.

    Blends play incoming material at the outgoing tempo, so `blend_beats`
    output beats eat blend_beats incoming beats; cuts play native, so the
    window spans blend_beats *outgoing* beats worth of wall time.
    """
    return params.blend_beats * (60.0 / in_bpm if params.template == "blend" else 60.0 / out_bpm)


_demucs_model = None


def _demucs_backend(audio: np.ndarray, sr: int) -> dict[str, np.ndarray]:
    """htdemucs via demucs 4.0.x's low-level API (the released PyPI version
    has no demucs.api). Normalization mirrors demucs/separate.py. Lazy
    import: torch takes seconds and the app must boot without it."""
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    global _demucs_model
    if _demucs_model is None:
        _demucs_model = get_model("htdemucs")
        _demucs_model.eval()
    model = _demucs_model
    if sr != model.samplerate:
        raise ValueError(f"expected {model.samplerate} Hz audio, got {sr}")

    wav = torch.from_numpy(np.ascontiguousarray(audio.T, dtype=np.float32))
    ref = wav.mean(0)
    scale = ref.std() + 1e-8
    wav = (wav - ref.mean()) / scale
    device = "cuda" if torch.cuda.is_available() else "cpu"
    with torch.no_grad():
        sources = apply_model(
            model, wav[None], device=device, shifts=1, split=True, overlap=0.25, progress=False
        )[0]
    sources = sources * scale + ref.mean()
    return {
        name: src.cpu().numpy().T.astype(np.float32)
        for name, src in zip(model.sources, sources)
    }


_backend = _demucs_backend  # module-level seam so tests can swap in a fake
