"""Application paths and settings.

Everything lives under DATA_DIR (default: <repo>/data, git-ignored).
Override with the MASHUP_DATA_DIR environment variable.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("MASHUP_DATA_DIR", REPO_ROOT / "data"))
DB_PATH = DATA_DIR / "library.sqlite3"
PROJECTS_DIR = DATA_DIR / "projects"
EXPORTS_DIR = DATA_DIR / "exports"
CACHE_DIR = DATA_DIR / "cache"  # reserved: per-track stems / stretched segments (Phase 2)

# ffmpeg/ffprobe are resolved from PATH; override for non-standard installs.
FFMPEG = os.environ.get("MASHUP_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("MASHUP_FFPROBE", "ffprobe")

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".m4a"}

ANALYSIS_SAMPLE_RATE = 22050  # analysis runs on downsampled mono audio
RENDER_SAMPLE_RATE = 44100  # rendering/export runs at full rate, stereo

# A track whose BPM lands below this is assumed to be a half-time detection
# error and gets doubled (hard dance lives roughly in 140-220).
MIN_PLAUSIBLE_BPM = 120.0


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROJECTS_DIR, EXPORTS_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
