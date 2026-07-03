# Mash-Up Maker

A local tool for crafting DJ-set-quality transitions between hardstyle / rawstyle /
hardcore tracks and rendering them into one continuous mix. See [DESIGN.md](DESIGN.md)
for the full design and phase plan.

## Architecture

- **`backend/`** — Python + FastAPI. Library scanning, audio analysis (BPM, beat grid,
  key, energy, sections), order suggestion, seam suggestion, DSP and final rendering.
- **`frontend/`** — Vite + React + TypeScript. Library view, set timeline, seam editor,
  Web Audio preview engine.
- **`data/`** (created at runtime, git-ignored) — SQLite library index, analysis cache,
  set projects (JSON), exported renders.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python + venv automatically)
- Node.js 20+
- ffmpeg on PATH (decode MP3/M4A, encode MP3 exports)

## Run (development)

Backend (from `backend/`):

```powershell
uv run uvicorn app.main:app --reload --port 8000
```

Frontend (from `frontend/`):

```powershell
npm install   # first time only
npm run dev
```

Open http://localhost:5173 — the Vite dev server proxies `/api` to the backend.

## Tests

```powershell
cd backend
uv run pytest
```

## Phase 1 status

- [x] Project scaffold (backend + frontend, wired together)
- [x] Library: folder registration, scan, background analysis queue
- [x] Analysis: BPM + constant beat grid, key → Camelot, energy (crude section detection: TODO improve)
- [x] Order suggestion (greedy over BPM/Camelot/energy)
- [x] Set projects (JSON, non-destructive)
- [x] Naive end-to-end export: WAV + MP3 + tracklist (hard cuts / equal-power crossfades only)
- [x] Seam editor UI: overlapped beat-aligned waveforms with draggable exit/window/entry,
  full-track overview strips (click to place exit/entry) + numeric time fields,
  blend/cut templates, volume + 3-band EQ curves, filter sweeps, tail FX params
  (persisted per seam; rendering honors template/points, curves render in the next milestone)
- [x] Seam suggestion exits at the last *full-energy* 32-beat phrase boundary
  (end of the last kick section, not the outro), with EQ bass-swap seeded for blends
- [x] Hybrid preview: server renders tempo-matched segments per seam; volume/EQ/filter/tail
  curves applied live via Web Audio with playhead (curve tweaks re-apply without re-render)
- [ ] Render engine: tempo-matched blends, EQ curves, filter sweeps, reverb/delay tails
- [ ] Manual grid/section correction UI
