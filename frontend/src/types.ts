// Mirrors backend/app/models.py — keep in sync by hand for now.
// TODO(dx): generate from the FastAPI OpenAPI schema instead.

export interface Section {
  label: string // intro | build | drop | break | outro
  start_sec: number
  end_sec: number
}

export interface Track {
  id: number
  path: string
  filename: string
  duration_sec: number | null
  analysis_status: 'pending' | 'running' | 'done' | 'error'
  analysis_error: string | null
  bpm: number | null
  key_name: string | null
  camelot: string | null
  energy: number | null
}

export interface Analysis {
  track_id: number
  bpm: number
  beat_offset_sec: number
  key_name: string | null
  camelot: string | null
  energy: number | null
  sections: Section[]
}

export interface Waveform {
  track_id: number
  bin_sec: number
  duration_sec: number
  peaks: number[]
}

// One automation point; linear interpolation between points, flat outside.
// `beat` counts from the start of that side's transition window (see models.py).
export interface CurvePoint {
  beat: number
  value: number
}

export interface FilterSweep {
  kind: 'off' | 'lowpass' | 'highpass'
  cutoff_hz: CurvePoint[]
}

export interface TailFX {
  kind: 'none' | 'reverb' | 'delay'
  wet: number
  time_beats: number
  feedback: number
}

export interface SideAutomation {
  volume: CurvePoint[]
  eq_low_db: CurvePoint[]
  eq_mid_db: CurvePoint[]
  eq_high_db: CurvePoint[]
  filter: FilterSweep
}

export interface SeamParams {
  template: 'blend' | 'cut'
  out_point_sec: number | null
  in_point_sec: number
  blend_beats: number
  out_auto: SideAutomation
  in_auto: SideAutomation
  tail: TailFX
}

export interface Seam {
  out_track_id: number
  in_track_id: number
  params: SeamParams
}

export interface Project {
  name: string
  track_ids: number[]
  seams: Seam[]
}

export interface AdjacencyScore {
  out_track_id: number
  in_track_id: number
  bpm_gap_pct: number
  camelot_distance: number | null
  score: number
}

export interface OrderSuggestion {
  track_ids: number[]
  adjacencies: AdjacencyScore[]
}

export interface SeamSuggestion {
  params: SeamParams
  rationale: string
}

// Hybrid-preview metadata; the client fetches the WAV segments and applies
// all automation itself via Web Audio.
export interface SeamPreviewOut {
  key: string
  sample_rate: number
  tau0_sec: number // preview t=0 expressed in outgoing-track time
  entry_sec: number // where the incoming segment starts, in preview time
  window_sec: number
  duration_sec: number
  out_url: string
  in_url: string
}

export interface ExportResult {
  wav_path: string
  mp3_path: string | null
  tracklist_path: string
  duration_sec: number
}
