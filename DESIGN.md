# Mash-Up Maker — Design Document

A tool for crafting DJ-set-quality transitions between hardstyle / rawstyle / hardcore
tracks, and rendering them into a continuous mix. Outcome of the /grill-me design
interview, 2026-07-02.

## Vision

Offline set editor: pick tracks, get an assisted-but-tweakable transition at every
seam, render one continuous audio file that sounds like a well-mixed hard-dance set.
Not a live DJ tool, not a DAW.

## Decisions (from the interview)

| # | Topic | Decision |
|---|-------|----------|
| 1 | End product | Rendered full set — one continuous audio file |
| 2 | Automation | Assisted + tweakable: tool analyzes and suggests, user adjusts everything |
| 3 | Techniques | Beat-aligned blend + EQ, hard cuts/slams, FX toolkit, stems (kick swap) — stems deferred to Phase 2 |
| 4 | Tempo | Stretch to common tempo when tracks are within ~8–10 %; tempo ramps across the seam for bigger gaps; hard cuts always allowed regardless of gap |
| 5 | Analysis | Built-in automatic (BPM, beat grid, downbeats, sections, key) with manual correction UI; no Rekordbox/Serato import in v1 |
| 6 | Stack | Local web app: Python backend + browser frontend at localhost |
| 7 | UI model | Set timeline (ordered lane of tracks) + click-a-seam transition editor with overlapped, beat-aligned waveforms |
| 8 | Preview | Hybrid: server pre-renders tempo-matched segments per seam; browser applies volume/EQ/filter/FX automation live via Web Audio for instant feedback; final export rendered server-side (server render is ground truth) |
| 9 | Suggestions | Structure heuristics: exit on phrase boundary after drop or in outro, enter at intro or slam into drop, always 16/32-beat aligned |
| 10 | Ordering | Tool **suggests** a track order (BPM/key/energy compatibility, greedy solver); user can freely reorder |
| 11 | Stems | Phase 2, after the core loop is proven; architecture keeps room (per-track cache, segment-based engine) |
| 12 | Library | Point at folder(s); background scan + analysis; per-track cache; scale target: hundreds of tracks; formats MP3/FLAC/WAV/M4A via ffmpeg |
| 13 | Export | WAV master + 320 kbps MP3 + timestamped tracklist (text/CUE) |
| 14 | MVP cut | Full loop with basic FX (filter sweeps, reverb/delay tail); sample-pack FX in Phase 1.5; stems in Phase 2 |

## Default technical choices (vetoable, not yet interviewed)

- **Backend:** Python 3.11+, FastAPI + uvicorn. ffmpeg for decode/encode.
- **Analysis:** librosa for BPM/beat/onset/chroma first; swap in a stronger downbeat
  tracker only if grid quality disappoints (manual grid nudge is the backstop).
  Hard dance tracks are produced on a constant grid, so one BPM + one anchor
  downbeat per track is the model.
- **Key detection:** chroma + Krumhansl–Schmuckler templates → Camelot notation
  (only needs to be good enough for compatibility ranking).
- **Time-stretch:** Rubber Band via `pedalboard.time_stretch` (pip-installable on
  Windows, no CLI binary needed).
- **DSP/FX (server render):** Spotify `pedalboard` (EQ, filters, reverb, delay) + numpy mixdown.
- **Preview DSP (client):** Web Audio native nodes only (gain, biquad, delay,
  convolver) so the two DSP paths stay alignable. Automatable parameters are
  limited to what Web Audio can do live.
- **Frontend:** Vite + React + TypeScript; wavesurfer.js (or custom canvas) for waveforms.
- **Persistence:** library index in SQLite; analysis cache keyed by file hash;
  a set project is a JSON file referencing library tracks (fully non-destructive).
- **Ordering solver:** greedy nearest-neighbor over (BPM distance, Camelot
  distance, energy), presented as a suggestion, drag-to-override.

## Phases

**Phase 1 (MVP):**
folder scan + background analysis → library view with BPM/key → suggested order →
set timeline → seam editor (blend & cut templates, transition point/length,
volume + 3-band EQ curves, low/high-pass sweeps, reverb/delay tail) → instant
hybrid preview → export WAV + MP3 + tracklist.

**Phase 1.5:** built-in sample pack (risers, impacts, crashes, white noise) placeable in the seam editor; adjacency warnings refinement.

**Phase 2:** stem separation (Demucs-class, cached per track) → kick swap, acapella-over-drop, melody-over-kick transitions in the seam editor.

## Known risks

1. **Two DSP paths (preview vs. render)** may sound slightly different — accepted;
   server render is the source of truth, param set is constrained to keep them close.
2. **Section/structure detection** is the weakest analysis link (drops/breakdowns);
   mitigated by manual section labeling in the UI and energy/novelty heuristics.
3. **Beat grid drift** in kickless intros/breakdowns — mitigated by constant-grid
   assumption + manual anchor nudge.
4. **Big tempo gaps** (150 → 190) blend poorly by nature; the tool prefers ramps or
   cuts there and should communicate why.
