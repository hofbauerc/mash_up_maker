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
- [x] Seam editor UI: overlapped beat-aligned waveforms with draggable exit/window/entry,
  full-track overview strips (click to place exit/entry) + numeric time fields,
  blend/cut templates, volume + 3-band EQ curves, filter sweeps, tail FX params
  (persisted per seam)
- [x] Seam suggestion exits at the last *full-energy* 32-beat phrase boundary
  (end of the last kick section, not the outro), enters on the incoming grid's
  first beat, with EQ bass-swap seeded for blends
- [x] Hybrid preview: server renders tempo-matched segments per seam; volume/EQ/filter/tail
  curves applied live via Web Audio with playhead (curve tweaks re-apply without re-render)
- [x] Render engine (export = WAV + MP3 + tracklist): grid-aware blends with the incoming
  track tempo-matched across the window then ramped back to native, volume/EQ curves,
  filter sweeps and reverb/delay tails mirroring the preview graph, declicked cuts
- [x] Manual grid/section correction UI: track inspector in the library — zoomable
  beat-grid waveform, BPM override (×2/÷2), anchor nudges, metronome grid-check
  playback (live while tweaking), section relabel/split/edit

## Phase 1.5 status

- [x] Built-in sample pack (riser, noise sweep, impact, crash) — procedurally
  synthesized (seeded, deterministic, nothing to license), beat-synced kinds
  generated at the outgoing tempo, WAVs cached under `data/cache/samples/`
- [x] Sample placement in the seam editor: place on the outgoing beat grid
  (beat 0 = window start; risers end-align to the exit by default), length /
  gain per placement, drawn on the seam waveform, persisted per seam
- [x] Samples play in the hybrid preview (scheduled Web Audio buffers, live
  re-apply on tweak) and render identically in the export mixdown
- [x] Adjacency warnings: BPM-gap / Camelot / energy-drop badges on every seam
  in the set timeline, plus a warning in the seam editor when a blend spans
  a >10 % BPM gap

## Phase 2 status

- [x] Stem separation: Demucs htdemucs (CPU or CUDA if available), one
  background job at a time, 4 stems cached per track under
  `data/cache/stems/{id}/`; triggered from the seam editor, resumed after
  restarts. First separation downloads the model weights (~80 MB)
- [x] Per-side stem mixes on every seam: drums/bass/vocals/other toggles
  applied across the transition window, rewritten in the source domain
  before any tempo-stretch so preview and export share the math
- [x] Stem transition presets: kick swap, melody over kick, acapella over
  drop (and full-mix reset)
- [x] Preview bakes the stem mix into the server segments (mix changes
  re-render; curve tweaks stay live); export renders identically or answers
  409 when a needed track isn't separated yet
