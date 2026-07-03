import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { PreviewEngine, segmentSignature, type LoadedPreview } from '../audio/previewEngine'
import { CurveLane } from '../components/CurveLane'
import { OverviewStrip } from '../components/OverviewStrip'
import { IN_COLOR, OUT_COLOR } from '../components/palette'
import { SeamWaveform } from '../components/SeamWaveform'
import type { Analysis, SeamParams, SideAutomation, Track, Waveform } from '../types'

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
        const pos = engine.position() // curve tweak: re-apply live from here
        engine.play(pv, next, data.outAnalysis.bpm, pos)
        tick(pv)
      }
    }
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
            applied to the preview live. Blends play the incoming track tempo-matched; the final
            export render honors curves in the next milestone.
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

function TimeInput({ valueSec, onCommit }: { valueSec: number; onCommit: (sec: number) => void }) {
  const [text, setText] = useState(fmtTime(valueSec))
  useEffect(() => {
    setText(fmtTime(valueSec))
  }, [valueSec])
  return (
    <input
      className="time-input"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        const parsed = parseTime(text)
        if (parsed == null) setText(fmtTime(valueSec))
        else onCommit(parsed)
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
      }}
    />
  )
}

function parseTime(text: string): number | null {
  const t = text.trim()
  const m = /^(\d+):(\d{1,2}(?:\.\d+)?)$/.exec(t)
  if (m) return Number(m[1]) * 60 + Number(m[2])
  if (/^\d+(\.\d+)?$/.test(t)) return Number(t)
  return null
}

function fmtTime(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = sec - m * 60
  return `${m}:${s.toFixed(1).padStart(4, '0')}`
}
