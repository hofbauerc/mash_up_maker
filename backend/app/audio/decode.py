"""Audio decoding via ffmpeg (handles MP3/FLAC/WAV/M4A uniformly)."""

import json
import subprocess

import numpy as np

from .. import config


class DecodeError(RuntimeError):
    pass


def decode(path: str, sample_rate: int, mono: bool) -> np.ndarray:
    """Decode any supported file to float32 PCM.

    Returns shape (n,) for mono, (n, 2) for stereo.
    """
    cmd = [
        config.FFMPEG,
        "-v", "error",
        "-i", path,
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "-ac", "1" if mono else "2",
        "-ar", str(sample_rate),
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as e:
        raise DecodeError(
            "ffmpeg not found on PATH (set MASHUP_FFMPEG to its full path)"
        ) from e
    if proc.returncode != 0:
        raise DecodeError(f"ffmpeg failed for {path}: {proc.stderr.decode(errors='replace')[:500]}")
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if not mono:
        audio = audio.reshape(-1, 2)
    return audio


def probe_duration_sec(path: str) -> float | None:
    cmd = [
        config.FFPROBE,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
        info = json.loads(proc.stdout or b"{}")
        return float(info["format"]["duration"])
    except Exception:
        return None
