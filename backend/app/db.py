"""SQLite library index and analysis cache.

Connections are opened per call — cheap, and it keeps the worker threads and
request handlers from sharing connection state.
"""

import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    duration_sec REAL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- pending | running | done | error
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    analysis_error TEXT
);

CREATE TABLE IF NOT EXISTS analysis (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    bpm REAL NOT NULL,
    -- constant-grid model: beat n is at beat_offset_sec + n * 60/bpm
    beat_offset_sec REAL NOT NULL,
    key_name TEXT,
    camelot TEXT,
    energy REAL,
    sections_json TEXT NOT NULL DEFAULT '[]',
    analyzed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    config.ensure_dirs()
    with connect() as conn:
        conn.executescript(SCHEMA)
