import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { PreviewEngine, segmentSignature, type LoadedPreview } from '../audio/previewEngine'
import { CurveLane } from '../components/CurveLane'
import { OverviewStrip } from '../components/OverviewStrip'
import { IN_BAND_COLORS, IN_COLOR, OUT_BAND_COLORS, OUT_COLOR } from '../components/palette'
import { SeamWaveform } from '../components/SeamWaveform'
import { TimeInput } from '../components/TimeInput'
import type {
  Analysis,
  SamplePlacement,
  SeamParams,
  SideAutomation,
  StemMix,
  StemsStatus,
  Track,
  Waveform,
} from '../types'

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
const SAMPLE_HINTS: Record<string, string> = {
  riser: 'A rising "whoosh" that builds tension toward the switch — ends right on the exit by default.',
  noise: 'A white-noise sweep that fills the background of the blend.',
  impact: 'A boom that marks the moment of the switch — lands on the exit by default.',
  crash: 'A cymbal wash that papers over the seam.',
}

// Stem transitions (Phase 2). Mixes apply across the transition window only;
// all-unity = passthrough (no separated stems needed).
const STEM_NAMES = ['drums', 'bass', 'vocals', 'other'] as const
const STEM_HINTS: Record<string, string> = {
  drums: 'the kick and all percussion',
  bass: 'the bassline',
  vocals: 'singing and shouts',
  other: 'melodies, leads, screeches — everything else',
}
const PASSTHROUGH: StemMix = { drums: 1, bass: 1, vocals: 1, other: 1 }
const STEM_PRESETS: { label: string; hint: string; needsIn: boolean; out: StemMix; in: StemMix }[] = [
  {
    label: 'kick swap',
    hint: 'Outgoing keeps everything but its kick; the incoming kick drives the window.',
    needsIn: true,
    out: { ...PASSTHROUGH, drums: 0 },
    in: { drums: 1, bass: 0, vocals: 0, other: 0 },
  },
  {
    label: 'melody over kick',
    hint: 'Outgoing melody + vocals ride over the incoming track through the window.',
    needsIn: false,
    out: { drums: 0, bass: 0, vocals: 1, other: 1 },
    in: PASSTHROUGH,
  },
  {
    label: 'acapella over drop',
    hint: 'Only the outgoing vocals carry across — land the exit on the incoming drop.',
    needsIn: false,
    out: { drums: 0, bass: 0, vocals: 1, other: 0 },
    in: PASSTHROUGH,
  },
  { label: 'full mix', hint: 'Reset both sides to the untouched masters.', needsIn: false, out: PASSTHROUGH, in: PASSTHROUGH },
]

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
  /** Set-trims from the project (auto gain / manual), applied in preview. */
  outGainDb?: number
  inGainDb?: number
  onCommit: (params: SeamParams) => void
}

export function SeamEditor({
  outTrack,
  inTrack,
  savedParams,
  outGainDb = 0,
  inGainDb = 0,
  onCommit,
}: SeamEditorProps) {
  const [data, setData] = useState<SeamData | null>(null)
  const [params, setParams] = useState<SeamParams | null>(null)
  const [rationale, setRationale] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [previewState, setPreviewState] = useState<'idle' | 'rendering' | 'playing'>('idle')
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [playheadTau, setPlayheadTau] = useState<number | null>(null)
  const [outStems, setOutStems] = useState<StemsStatus | null>(null)
  const [inStems, setInStems] = useState<StemsStatus | null>(null)
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
      api.getStems(outTrack.id),
      api.getStems(inTrack.id),
    ])
      .then(([outAnalysis, inAnalysis, outWave, inWave, suggestion, outSt, inSt]) => {
        if (cancelled) return
        setData({ outAnalysis, inAnalysis, outWave, inWave })
        setOutStems(outSt)
        setInStems(inSt)
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

  // Poll separation progress while either track's stems job is in flight.
  const stemsInFlight = [outStems?.status, inStems?.status].some(
    (s) => s === 'pending' || s === 'running',
  )
  useEffect(() => {
    if (!stemsInFlight) return
    const timer = setInterval(() => {
      void api.getStems(outTrack.id).then(setOutStems).catch(() => {})
      void api.getStems(inTrack.id).then(setInStems).catch(() => {})
    }, 3000)
    return () => clearInterval(timer)
  }, [stemsInFlight, outTrack.id, inTrack.id])

  function requestStems(side: 'out' | 'in') {
    const id = side === 'out' ? outTrack.id : inTrack.id
    const set = side === 'out' ? setOutStems : setInStems
    api
      .requestStems(id)
      .then(set)
      .catch((e) => setPreviewError(String(e)))
  }

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
      engine.play(pv, params, data.outAnalysis.bpm, 0, { out: outGainDb, in: inGainDb })
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
            engine.play(pv, next, data.outAnalysis.bpm, pos, { out: outGainDb, in: inGainDb })
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
                    <button
                      title="Play this transition with everything applied — curves, FX, samples, stems. The first play renders audio on the server; tweaks afterwards are instant."
                      onClick={() => void playPreview()}
                      disabled={previewState === 'rendering'}
                    >
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
                        title={
                          t === 'blend'
                            ? 'Both tracks play together across the window, tempo-matched — the classic smooth DJ mix. Best under a ~10% BPM gap.'
                            : 'Hard switch at the exit point — instant and clean, works for any BPM gap.'
                        }
                        onClick={() => update({ ...params, template: t }, true)}
                      >
                        {t}
                      </button>
                    ))}
                  </span>
                  <label title="How long the transition lasts, in beats of the outgoing track (32 beats = 8 bars ≈ one hard-dance phrase).">
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
                  <label title="Where the outgoing (old) track stops playing — the transition ends here.">
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
                  <label title="Where playback starts inside the incoming (new) track.">
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
                    title="Throw away your edits on this seam and restore the tool's automatic suggestion."
                    onClick={() => suggested.current && update(suggested.current, true)}
                    disabled={!suggested.current}
                  >
                    Re-apply suggestion
                  </button>
                  <button
                    title="Re-seed the EQ curves for the CURRENT points and window by analyzing what actually plays: the bass swap lands where the incoming kick starts, mids dip only where melodies clash. Your volume and filter curves stay untouched — and everything remains editable."
                    onClick={() => {
                      api
                        .autoEq(outTrack.id, inTrack.id, params)
                        .then((r) => {
                          update({ ...params, out_auto: r.out_auto, in_auto: r.in_auto }, true)
                          setRationale(r.rationale)
                        })
                        .catch((e) => setPreviewError(String(e)))
                    }}
                  >
                    Auto EQ
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
                  bandColors={OUT_BAND_COLORS}
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
                  bandColors={IN_BAND_COLORS}
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

          <div className="tail-row">
            <span title="One-click recipes using the AI-separated instrument layers — tricks EQ alone can't do, like swapping only the kick.">
              Stem transitions:
            </span>
            <span className="chip-row">
              {STEM_PRESETS.map((p) => {
                const ready =
                  p.label === 'full mix' ||
                  (outStems?.status === 'done' && (!p.needsIn || inStems?.status === 'done'))
                return (
                  <button
                    key={p.label}
                    className="chip"
                    title={ready ? p.hint : `${p.hint} (separate stems first)`}
                    disabled={!ready}
                    onClick={() => update({ ...params, out_stems: p.out, in_stems: p.in }, true)}
                  >
                    {p.label}
                  </button>
                )
              })}
            </span>
            <span className="muted">
              stem mixes act across the window; toggling re-renders the preview segments
            </span>
          </div>

          <div className="auto-grid">
            <SidePanel
              title={`Outgoing — ${outTrack.filename}`}
              auto={params.out_auto}
              blendBeats={params.blend_beats}
              volumeHint={params.template === 'blend' ? 'fade out' : 'full until cut'}
              stems={outStems}
              mix={params.out_stems ?? PASSTHROUGH}
              onMix={(mix) => update({ ...params, out_stems: mix }, true)}
              onRequestStems={() => requestStems('out')}
              onChange={(auto, commit) => update({ ...params, out_auto: auto }, commit)}
            />
            <SidePanel
              title={`Incoming — ${inTrack.filename}`}
              auto={params.in_auto}
              blendBeats={params.blend_beats}
              volumeHint={params.template === 'blend' ? 'fade in' : 'full from cut'}
              stems={inStems}
              mix={params.in_stems ?? PASSTHROUGH}
              onMix={(mix) => update({ ...params, in_stems: mix }, true)}
              onRequestStems={() => requestStems('in')}
              onChange={(auto, commit) => update({ ...params, in_auto: auto }, commit)}
            />
          </div>

          <div className="tail-row">
            <span title="Lets the old track ring out into the new one instead of stopping dead — smooths hard cuts.">
              Tail FX (outgoing):
            </span>
            <span className="chip-row">
              {(['none', 'reverb', 'delay'] as const).map((k) => (
                <button
                  key={k}
                  className={`chip ${params.tail.kind === k ? 'active' : ''}`}
                  title={
                    k === 'none'
                      ? 'No tail — the outgoing track just stops at the exit.'
                      : k === 'reverb'
                        ? 'The exit washes out in a big room echo under the new track.'
                        : 'The last beats repeat as fading echoes under the new track.'
                  }
                  onClick={() => update({ ...params, tail: { ...params.tail, kind: k } }, true)}
                >
                  {k}
                </button>
              ))}
            </span>
            {params.tail.kind !== 'none' && (
              <label title="How loud the effect tail is.">
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
                <label title="Spacing between the echoes, in beats.">
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
                <label title="How long the echoes keep repeating before dying out.">
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
            <span title="One-shot sounds placed on the beat grid to glue the transition together — the polish layer of a produced-sounding mix.">
              Samples:
            </span>
            <span className="chip-row">
              {SAMPLE_KINDS.map(({ kind, beatSynced }) => (
                <button
                  key={kind}
                  className="chip"
                  title={SAMPLE_HINTS[kind]}
                  onClick={() => addSample(kind, beatSynced)}
                >
                  + {kind}
                </button>
              ))}
            </span>
            {(params.samples ?? []).map((s, i) => (
              <span className="sample-item" key={i}>
                <strong title={SAMPLE_HINTS[s.kind]}>{s.kind}</strong>
                <label title={`When the sample starts, in beats from the window start (negative = before the window, ${params.blend_beats} = the exit).`}>
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
                  <label title="How many beats the sample lasts — it is generated at this seam's tempo to fit exactly.">
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
                <label title="Sample volume: 0 dB = full blast, negative = quieter.">
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
  stems,
  mix,
  onMix,
  onRequestStems,
  onChange,
}: {
  title: string
  auto: SideAutomation
  blendBeats: number
  volumeHint: string
  stems: StemsStatus | null
  mix: StemMix
  onMix: (mix: StemMix) => void
  onRequestStems: () => void
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
    hint?: string,
  ) => (
    <CurveLane
      label={label}
      hint={hint}
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
      {lane(
        `volume (${volumeHint})`,
        'volume',
        0,
        1,
        1,
        (v) => v.toFixed(2),
        'How loud this track is across the transition (1 = full, 0 = silent). Leave empty for an automatic smooth fade. Click the lane to add a point, drag to shape, double-click to remove.',
      )}
      {lane(
        'EQ low',
        'eq_low_db',
        -26,
        6,
        0,
        eqFormat,
        'The bass region — kick and bassline live here. Two basslines at once sound muddy, so DJs "kill" one side: pull this to −26 dB while the other track keeps its bass.',
      )}
      {lane(
        'EQ mid',
        'eq_mid_db',
        -26,
        6,
        0,
        eqFormat,
        'The middle frequencies — melodies, vocals, screeches. Dip one side to keep two melodies from fighting.',
      )}
      {lane(
        'EQ high',
        'eq_high_db',
        -26,
        6,
        0,
        eqFormat,
        'The treble — hi-hats, cymbals, sparkle. Cutting it makes a track sound dull and distant.',
      )}
      <div className="chip-row">
        <span
          className="lane-label"
          title="A filter sweep — the classic DJ 'wah' effect. Pick a type, then draw where the cutoff moves over the window."
        >
          filter
        </span>
        {(['off', 'lowpass', 'highpass'] as const).map((k) => (
          <button
            key={k}
            className={`chip ${auto.filter.kind === k ? 'active' : ''}`}
            title={
              k === 'off'
                ? 'No filter sweep on this side.'
                : k === 'lowpass'
                  ? 'Low-pass: sweeping the cutoff down makes the track sound muffled / underwater — a classic way to ease a track out.'
                  : 'High-pass: sweeping the cutoff up thins the track to just its highs — makes room for the other track.'
            }
            onClick={() => onChange({ ...auto, filter: { ...auto.filter, kind: k } }, true)}
          >
            {k === 'lowpass' ? 'LP' : k === 'highpass' ? 'HP' : 'off'}
          </button>
        ))}
      </div>
      <div className="chip-row">
        <span
          className="lane-label"
          title="Stems are the track's instrument layers, pulled apart by AI. Choose which layers of this track play during the transition window — e.g. mute its drums so the other track's kick takes over."
        >
          stems
        </span>
        {stems?.status === 'done' ? (
          STEM_NAMES.map((n) => (
            <button
              key={n}
              className={`chip ${mix[n] > 0 ? 'active' : ''}`}
              title={`${n} = ${STEM_HINTS[n]}. Currently ${mix[n] > 0 ? 'playing' : 'muted'} in the transition window — click to toggle.`}
              onClick={() => onMix({ ...mix, [n]: mix[n] > 0 ? 0 : 1 })}
            >
              {n}
            </button>
          ))
        ) : stems?.status === 'pending' || stems?.status === 'running' ? (
          <span className="muted">separating… (takes minutes, runs in the background)</span>
        ) : stems?.status === 'error' ? (
          <>
            <span className="error" title={stems.error ?? undefined}>
              separation failed
            </span>
            <button className="chip" onClick={onRequestStems}>
              retry
            </button>
          </>
        ) : (
          <button
            className="chip"
            title="Let AI split this track into drums / bass / vocals / rest. Runs in the background, takes a few minutes, and is done once per track — it unlocks the stem toggles and presets."
            onClick={onRequestStems}
          >
            separate stems
          </button>
        )}
      </div>
      {auto.filter.kind !== 'off' && (
        <CurveLane
          label={`${auto.filter.kind} cutoff`}
          hint="Where the filter bites, over time. Low-pass: everything above this frequency is removed. High-pass: everything below."
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

