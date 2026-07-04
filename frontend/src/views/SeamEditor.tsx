import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { PreviewEngine, segmentSignature, type LoadedPreview } from '../audio/previewEngine'
import { CurveLane } from '../components/CurveLane'
import { OverviewStrip } from '../components/OverviewStrip'
import { IN_COLOR, OUT_COLOR } from '../components/palette'
import { SeamWaveform } from '../components/SeamWaveform'
import { TimeInput } from '../components/TimeInput'
import type { Analysis, SamplePlacement, SeamParams, SideAutomation, Track, Waveform } from '../types'

// The heart of the tool (DESIGN.md #7): overlapped beat-aligned waveforms,
// template selector, draggable transition point/length, volume + 3-band EQ
// curves, filter sweeps, reverb/delay tail — with instant hybrid preview
// (server-rendered segments, Web Audio automation).
//
// Opening a seam with no saved params materializes the backend's heuristic
// suggestion into the project ("assisted but tweakable"); every edit is
// committed back into the project's seams list.

const BLEND_LENGTHS = [4, 8, 16, 32, 48, 64, 96, 128]
const DELAY_TIMES = [0.25, 0.5, 0.75, 1, 1.5, 2]
// Built-in sample pack (Phase 1.5); beat-synced kinds span `beats` at the
// outgoing tempo, one-shots (impact/crash) have a fixed length.
const SAMPLE_KINDS = [
  { kind: 'riser', beatSynced: true },
  { kind: 'noise', beatSynced: true },
  { kind: 'impact', beatSynced: false },
  { kind: 'crash', beatSynced: false },
] as const
const SAMPLE_LENGTHS = [4, 8, 16, 32, 64]
const isBeatSynced = (kind: string) => SAMPLE_KINDS.some((k) => k.kind === kind && k.beatSynced)

interface SeamData {
  outAnalysis: Analysis
  inAnalysis: Analysis
  outWave: Waveform
  inWave: Waveform
}

type CachedPreview = LoadedPreview & { sig: string }

interface SeamEditorProps {
  outTrack: Track
  inTrack: Track
  savedParams: SeamParams | null
  onCommit: (params: SeamParams) => void
}

export function SeamEditor({ outTrack, inTrack, savedParams, onCommit }: SeamEditorProps) {
  const [data, setData] = useState<SeamData | null>(null)
  const [params, setParams] = useState<SeamParams | null>(null)
  const [rationale, setRationale] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [previewState, setPreviewState] = useState<'idle' | 'rendering' | 'playing'>('idle')
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [playheadTau, setPlayheadTau] = useState<number | null>(null)
  const suggested = useRef<SeamParams | null>(null)

  const engineRef = useRef<PreviewEngine | null>(null)
  const previewRef = useRef<CachedPreview | null>(null)
  const rafRef = useRef(0)

  // Read through refs inside the load effect so it depends on the pair only.
  const savedRef = useRef(savedParams)
  savedRef.current = savedParams
  const commitRef = useRef(onCommit)
  commitRef.current = onCommit

  useEffect(() => {
    let cancelled = false
    setData(null)
    setParams(null)
    setRationale(null)
    setError(null)
    Promise.all([
      api.getAnalysis(outTrack.id),
      api.getAnalysis(inTrack.id),
      api.getPeaks(outTrack.id),
      api.getPeaks(inTrack.id),
      api.suggestSeam(outTrack.id, inTrack.id),
    ])
      .then(([outAnalysis, inAnalysis, outWave, inWave, suggestion]) => {
        if (cancelled) return
        setData({ outAnalysis, inAnalysis, outWave, inWave })
        setRationale(suggestion.rationale)
        suggested.current = suggestion.params
        setParams(savedRef.current ?? suggestion.params)
        if (!savedRef.current) commitRef.current(suggestion.params)
      })
      .catch((e) => {
        if (!cancelled) setError(String(e))
      })
    return () => {
      cancelled = true
    }
  }, [outTrack.id, inTrack.id])

  useEffect(
    () => () => {
      cancelAnimationFrame(rafRef.current)
      void engineRef.current?.close()
    },
    [],
  )

  function stopPreview() {
    engineRef.current?.stop()
    cancelAnimationFrame(rafRef.current)
    setPlayheadTau(null)
    setPreviewState('idle')
  }

  function tick(pv: CachedPreview) {
    cancelAnimationFrame(rafRef.current)
    const step = () => {
      const engine = engineRef.current
      if (!engine?.playing) return
      const pos = engine.position()
      if (pos >= pv.meta.duration_sec) {
        stopPreview()
        return
      }
      setPlayheadTau(pv.meta.tau0_sec + pos)
      rafRef.current = requestAnimationFrame(step)
    }
    rafRef.current = requestAnimationFrame(step)
  }

  async function playPreview() {
    if (!data || !params) return
    const engine = (engineRef.current ??= new PreviewEngine())
    setPreviewError(null)
    try {
      let pv = previewRef.current
      const sig = segmentSignature(params)
      if (!pv || pv.sig !== sig) {
        setPreviewState('rendering')
        const meta = await api.renderSeamPreview(outTrack.id, inTrack.id, params)
        pv = { ...(await engine.load(meta)), sig }
        previewRef.current = pv
      }
      await engine.prime(params, data.outAnalysis.bpm)
      engine.play(pv, params, data.outAnalysis.bpm, 0)
      setPreviewState('playing')
      tick(pv)
    } catch (e) {
      setPreviewState('idle')
      setPreviewError(String(e))
    }
  }

  function update(next: SeamParams, commit: boolean) {
    setParams(next)
    if (commit) onCommit(next)
    const engine = engineRef.current
    const pv = previewRef.current
    if (engine?.playing && pv && data) {
      if (segmentSignature(next) !== pv.sig) {
        stopPreview() // cut points changed: segments are stale, re-render on next play
      } else if (commit) {
        // Curve/sample tweak: re-apply live from the current position. Prime
        // resolves instantly unless a newly placed sample is still fetching.
        void engine
          .prime(next, data.outAnalysis.bpm)
          .then(() => {
            if (!engine.playing) return
            const pos = engine.position()
            engine.play(pv, next, data.outAnalysis.bpm, pos)
            tick(pv)
          })
          .catch((e) => setPreviewError(String(e)))
      }
    }
  }

  function updateSample(i: number, patch: Partial<SamplePlacement>) {
    if (!params) return
    const list = [...(params.samples ?? [])]
    list[i] = { ...list[i], ...patch }
    update({ ...params, samples: list }, true)
  }

  function addSample(kind: string, beatSynced: boolean) {
    if (!params) return
    // Beat-synced samples default to ending at the exit (the drop); one-shots
    // default to hitting exactly on it.
    const beats = Math.min(16, params.blend_beats)
    const placement: SamplePlacement = beatSynced
      ? { kind, beat: params.blend_beats - beats, beats, gain_db: -6 }
      : { kind, beat: params.blend_beats, beats: 16, gain_db: -6 }
    update({ ...params, samples: [...(params.samples ?? []), placement] }, true)
  }

  const snapToGrid = (sec: number, a: Analysis) => {
    const beat = 60 / a.bpm
    const snapped = a.beat_offset_sec + Math.round((sec - a.beat_offset_sec) / beat) * beat
    return Math.round(snapped * 1000) / 1000
  }
  const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)

  return (
    <div className="seam-editor">
      <h3>
        Transition: {outTrack.filename} → {inTrack.filename}
      </h3>
      {error && <p className="error">{error} — both tracks must be analyzed.</p>}
      {!error && !(data && params) && <p className="muted">Loading analysis + waveforms…</p>}
      {data && params && (
        <>
          {(() => {
            const beatOut = 60 / data.outAnalysis.bpm
            const windowSec = params.blend_beats * beatOut
            const outPoint = params.out_point_sec ?? data.outWave.duration_sec
            const entryTau = params.template === 'blend' ? outPoint - windowSec : outPoint
            const inPlayhead =
              playheadTau != null ? params.in_point_sec + (playheadTau - entryTau) : null
            return (
              <>
                <div className="toolbar seam-toolbar">
                  {previewState !== 'playing' ? (
                    <button onClick={() => void playPreview()} disabled={previewState === 'rendering'}>
                      {previewState === 'rendering' ? 'Rendering…' : '▶ Preview'}
                    </button>
                  ) : (
                    <button onClick={stopPreview}>■ Stop</button>
                  )}
                  <span className="chip-row">
                    {(['blend', 'cut'] as const).map((t) => (
                      <button
                        key={t}
                        className={`chip ${params.template === t ? 'active' : ''}`}
                        onClick={() => update({ ...params, template: t }, true)}
                      >
                        {t}
                      </button>
                    ))}
                  </span>
                  <label>
                    window{' '}
                    <select
                      value={params.blend_beats}
                      onChange={(e) => update({ ...params, blend_beats: Number(e.target.value) }, true)}
                    >
                      {BLEND_LENGTHS.map((b) => (
                        <option key={b} value={b}>
                          {b} beats
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    exit{' '}
                    <TimeInput
                      valueSec={outPoint}
                      onCommit={(sec) =>
                        update(
                          {
                            ...params,
                            out_point_sec: clamp(
                              snapToGrid(sec, data.outAnalysis),
                              4 * beatOut,
                              data.outWave.duration_sec,
                            ),
                          },
                          true,
                        )
                      }
                    />
                  </label>
                  <label>
                    in from{' '}
                    <TimeInput
                      valueSec={params.in_point_sec}
                      onCommit={(sec) =>
                        update(
                          {
                            ...params,
                            in_point_sec: clamp(
                              snapToGrid(sec, data.inAnalysis),
                              0,
                              Math.max(0, data.inWave.duration_sec - 1),
                            ),
                          },
                          true,
                        )
                      }
                    />
                  </label>
                  <button
                    onClick={() => suggested.current && update(suggested.current, true)}
                    disabled={!suggested.current}
                  >
                    Re-apply suggestion
                  </button>
                  {previewError && <span className="error">{previewError}</span>}
                </div>

                {(() => {
                  const gapPct =
                    (Math.abs(data.outAnalysis.bpm - data.inAnalysis.bpm) / data.outAnalysis.bpm) * 100
                  return params.template === 'blend' && gapPct > 10 ? (
                    <p className="warn-note">
                      ⚠ {gapPct.toFixed(1)}% BPM gap — a blend this wide stretches audibly; consider a
                      cut (or slam into the drop).
                    </p>
                  ) : null
                })()}

                <OverviewStrip
                  label={`outgoing full track — click to place exit`}
                  wave={data.outWave}
                  analysis={data.outAnalysis}
                  markerSec={outPoint}
                  windowSec={windowSec}
                  windowSide="before"
                  color={OUT_COLOR}
                  playheadSec={playheadTau}
                  onChange={(sec, commit) => update({ ...params, out_point_sec: sec }, commit)}
                />
                <OverviewStrip
                  label={`incoming full track — click to place entry`}
                  wave={data.inWave}
                  analysis={data.inAnalysis}
                  markerSec={params.in_point_sec}
                  windowSec={windowSec}
                  windowSide="after"
                  color={IN_COLOR}
                  playheadSec={inPlayhead}
                  onChange={(sec, commit) => update({ ...params, in_point_sec: sec }, commit)}
                />

                <SeamWaveform
                  outWave={data.outWave}
                  inWave={data.inWave}
                  outAnalysis={data.outAnalysis}
                  inAnalysis={data.inAnalysis}
                  params={params}
                  playheadTau={playheadTau}
                  onChange={update}
                />
              </>
            )
          })()}
          <p className="muted hint">
            Overviews are bar-snapped for big jumps; below, drag the <strong>exit</strong> line
            (snaps to beats), the window edge (4-beat steps), or the incoming lane (snaps to its
            beats). Curves: click to add a point, drag to move, double-click to remove — tweaks are
            applied to the preview live. Blends play the incoming track tempo-matched; the export
            renders the same curves, sweeps and tail FX server-side.
          </p>

          <div className="auto-grid">
            <SidePanel
              title={`Outgoing — ${outTrack.filename}`}
              auto={params.out_auto}
              blendBeats={params.blend_beats}
              volumeHint={params.template === 'blend' ? 'fade out' : 'full until cut'}
              onChange={(auto, commit) => update({ ...params, out_auto: auto }, commit)}
            />
            <SidePanel
              title={`Incoming — ${inTrack.filename}`}
              auto={params.in_auto}
              blendBeats={params.blend_beats}
              volumeHint={params.template === 'blend' ? 'fade in' : 'full from cut'}
              onChange={(auto, commit) => update({ ...params, in_auto: auto }, commit)}
            />
          </div>

          <div className="tail-row">
            <span>Tail FX (outgoing):</span>
            <span className="chip-row">
              {(['none', 'reverb', 'delay'] as const).map((k) => (
                <button
                  key={k}
                  className={`chip ${params.tail.kind === k ? 'active' : ''}`}
                  onClick={() => update({ ...params, tail: { ...params.tail, kind: k } }, true)}
                >
                  {k}
                </button>
              ))}
            </span>
            {params.tail.kind !== 'none' && (
              <label>
                wet {Math.round(params.tail.wet * 100)}%{' '}
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={params.tail.wet}
                  onChange={(e) =>
                    update({ ...params, tail: { ...params.tail, wet: Number(e.target.value) } }, true)
                  }
                />
              </label>
            )}
            {params.tail.kind === 'delay' && (
              <>
                <label>
                  time{' '}
                  <select
                    value={params.tail.time_beats}
                    onChange={(e) =>
                      update({ ...params, tail: { ...params.tail, time_beats: Number(e.target.value) } }, true)
                    }
                  >
                    {DELAY_TIMES.map((t) => (
                      <option key={t} value={t}>
                        {t} beat{t === 1 ? '' : 's'}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  feedback {Math.round(params.tail.feedback * 100)}%{' '}
                  <input
                    type="range"
                    min={0}
                    max={0.9}
                    step={0.05}
                    value={params.tail.feedback}
                    onChange={(e) =>
                      update({ ...params, tail: { ...params.tail, feedback: Number(e.target.value) } }, true)
                    }
                  />
                </label>
              </>
            )}
          </div>

          <div className="tail-row samples-row">
            <span>Samples:</span>
            <span className="chip-row">
              {SAMPLE_KINDS.map(({ kind, beatSynced }) => (
                <button key={kind} className="chip" onClick={() => addSample(kind, beatSynced)}>
                  + {kind}
                </button>
              ))}
            </span>
            {(params.samples ?? []).map((s, i) => (
              <span className="sample-item" key={i}>
                <strong>{s.kind}</strong>
                <label>
                  start beat{' '}
                  <input
                    type="number"
                    step={1}
                    min={-16}
                    max={params.blend_beats + 32}
                    value={s.beat}
                    onChange={(e) => {
                      const v = Number(e.target.value)
                      if (Number.isFinite(v)) updateSample(i, { beat: v })
                    }}
                  />
                </label>
                {isBeatSynced(s.kind) && (
                  <label>
                    len{' '}
                    <select
                      value={s.beats}
                      onChange={(e) => updateSample(i, { beats: Number(e.target.value) })}
                    >
                      {SAMPLE_LENGTHS.map((b) => (
                        <option key={b} value={b}>
                          {b} beats
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label>
                  gain {s.gain_db} dB{' '}
                  <input
                    type="range"
                    min={-24}
                    max={6}
                    step={1}
                    value={s.gain_db}
                    onChange={(e) => updateSample(i, { gain_db: Number(e.target.value) })}
                  />
                </label>
                <button
                  onClick={() =>
                    update({ ...params, samples: (params.samples ?? []).filter((_, j) => j !== i) }, true)
                  }
                >
                  ✕
                </button>
              </span>
            ))}
            {(params.samples ?? []).length === 0 && (
              <span className="muted">
                risers/noise end-align to the exit by default; beat 0 = window start
              </span>
            )}
          </div>

          {rationale && <p className="muted">Suggestion: {rationale}</p>}
        </>
      )}
    </div>
  )
}

function SidePanel({
  title,
  auto,
  blendBeats,
  volumeHint,
  onChange,
}: {
  title: string
  auto: SideAutomation
  blendBeats: number
  volumeHint: string
  onChange: (auto: SideAutomation, commit: boolean) => void
}) {
  const eqFormat = (v: number) => `${v.toFixed(1)} dB`
  const hzFormat = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)} kHz` : `${v.toFixed(0)} Hz`)
  const lane = (
    label: string,
    key: 'volume' | 'eq_low_db' | 'eq_mid_db' | 'eq_high_db',
    min: number,
    max: number,
    def: number,
    format: (v: number) => string,
  ) => (
    <CurveLane
      label={label}
      points={auto[key]}
      blendBeats={blendBeats}
      min={min}
      max={max}
      defaultValue={def}
      format={format}
      onChange={(points, commit) => onChange({ ...auto, [key]: points }, commit)}
    />
  )

  return (
    <div className="auto-side">
      <h4>{title}</h4>
      {lane(`volume (${volumeHint})`, 'volume', 0, 1, 1, (v) => v.toFixed(2))}
      {lane('EQ low', 'eq_low_db', -26, 6, 0, eqFormat)}
      {lane('EQ mid', 'eq_mid_db', -26, 6, 0, eqFormat)}
      {lane('EQ high', 'eq_high_db', -26, 6, 0, eqFormat)}
      <div className="chip-row">
        <span className="lane-label">filter</span>
        {(['off', 'lowpass', 'highpass'] as const).map((k) => (
          <button
            key={k}
            className={`chip ${auto.filter.kind === k ? 'active' : ''}`}
            onClick={() => onChange({ ...auto, filter: { ...auto.filter, kind: k } }, true)}
          >
            {k === 'lowpass' ? 'LP' : k === 'highpass' ? 'HP' : 'off'}
          </button>
        ))}
      </div>
      {auto.filter.kind !== 'off' && (
        <CurveLane
          label={`${auto.filter.kind} cutoff`}
          points={auto.filter.cutoff_hz}
          blendBeats={blendBeats}
          min={20}
          max={20000}
          defaultValue={auto.filter.kind === 'lowpass' ? 20000 : 20}
          log
          format={hzFormat}
          onChange={(points, commit) => onChange({ ...auto, filter: { ...auto.filter, cutoff_hz: points } }, commit)}
        />
      )}
    </div>
  )
}

