import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { GridCheckPlayer } from '../audio/gridCheckPlayer'
import { IN_COLOR, SECTION_COLORS } from '../components/palette'
import { TimeInput } from '../components/TimeInput'
import { useElementWidth } from '../components/useElementWidth'
import type { Analysis, Section, Track, Waveform } from '../types'

// Manual grid/section correction (DESIGN.md #5): the constant-grid model is
// one BPM + one anchor offset, so fixing a track means nudging those two
// numbers until the metronome clicks sit on the kicks — the zoomed waveform
// shows the grid, the grid-check playback makes it audible. Sections can be
// relabeled/re-drawn for the seam suggester. Corrections persist via PATCH
// and immediately feed ordering, suggestions and the render.

const SECTION_LABELS = ['intro', 'build', 'drop', 'break', 'outro']
const ZOOMS = [2, 4, 8, 16, 32]
const OVERVIEW_H = 46
const DETAIL_H = 110
const STRIP = 5

interface GridDraft {
  bpm: number
  beat_offset_sec: number
  sections: Section[]
}

const draftOf = (a: Analysis): GridDraft => ({
  bpm: a.bpm,
  beat_offset_sec: a.beat_offset_sec,
  sections: a.sections.map((s) => ({ ...s })),
})

export function TrackInspector({ track, onSaved }: { track: Track; onSaved: () => void }) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [wave, setWave] = useState<Waveform | null>(null)
  const [draft, setDraft] = useState<GridDraft | null>(null)
  const [viewStart, setViewStart] = useState(0)
  const [viewLen, setViewLen] = useState(8)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [playhead, setPlayhead] = useState<number | null>(null)
  const [playing, setPlaying] = useState(false)

  const playerRef = useRef<GridCheckPlayer | null>(null)
  const rafRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    setAnalysis(null)
    setWave(null)
    setDraft(null)
    setError(null)
    setStatus(null)
    stopPlayback()
    Promise.all([api.getAnalysis(track.id), api.getPeaks(track.id, 200)])
      .then(([a, w]) => {
        if (cancelled) return
        setAnalysis(a)
        setWave(w)
        setDraft(draftOf(a))
        // Open the view on the first drop — that's where the kicks are.
        const drop = a.sections.find((s) => s.label === 'drop')
        setViewStart(drop ? drop.start_sec : 0)
      })
      .catch((e) => {
        if (!cancelled) setError(String(e))
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [track.id])

  useEffect(
    () => () => {
      cancelAnimationFrame(rafRef.current)
      void playerRef.current?.close()
    },
    [],
  )

  function stopPlayback() {
    playerRef.current?.stop()
    cancelAnimationFrame(rafRef.current)
    setPlayhead(null)
    setPlaying(false)
  }

  function tick() {
    cancelAnimationFrame(rafRef.current)
    const step = () => {
      const player = playerRef.current
      if (!player?.playing) return
      const pos = player.position()
      if (player.duration != null && pos >= player.duration) {
        stopPlayback()
        return
      }
      setPlayhead(pos)
      // Follow the playhead once it runs off the right edge of the view.
      setViewStart((v) => (pos > v + viewLen ? pos : v))
      rafRef.current = requestAnimationFrame(step)
    }
    rafRef.current = requestAnimationFrame(step)
  }

  async function playFrom(sec: number) {
    if (!draft) return
    const player = (playerRef.current ??= new GridCheckPlayer())
    try {
      setStatus('loading audio…')
      await player.load(track.id)
      setStatus(null)
      player.play(sec, draft.bpm, draft.beat_offset_sec)
      setPlaying(true)
      tick()
    } catch (e) {
      setStatus(null)
      setError(String(e))
    }
  }

  /** Apply a grid change; when playing, restart in place so the new grid is audible. */
  function updateGrid(next: GridDraft) {
    setDraft(next)
    setStatus(null)
    const player = playerRef.current
    if (player?.playing) {
      player.play(player.position(), next.bpm, next.beat_offset_sec)
    }
  }

  async function save() {
    if (!draft) return
    try {
      setStatus('saving…')
      const updated = await api.patchAnalysis(track.id, {
        bpm: draft.bpm,
        beat_offset_sec: draft.beat_offset_sec,
        sections: draft.sections,
      })
      setAnalysis(updated)
      setDraft(draftOf(updated))
      setStatus('saved ✓')
      onSaved()
    } catch (e) {
      setStatus(null)
      setError(String(e))
    }
  }

  if (error) return <div className="inspector"><p className="error">{error}</p></div>
  if (!analysis || !wave || !draft) {
    return <div className="inspector"><p className="muted">Loading analysis + waveform…</p></div>
  }

  const beat = 60 / draft.bpm
  const fold = (v: number) => ((v % beat) + beat) % beat
  const dirty = JSON.stringify(draft) !== JSON.stringify(draftOf(analysis))
  const dur = wave.duration_sec
  const clampView = (v: number) => Math.min(Math.max(v, 0), Math.max(0, dur - viewLen))

  return (
    <div className="inspector">
      <h3>Grid &amp; sections — {track.filename}</h3>
      <div className="toolbar">
        {!playing ? (
          <button onClick={() => void playFrom(viewStart)}>▶ Grid check</button>
        ) : (
          <button onClick={stopPlayback}>■ Stop</button>
        )}
        <label>
          BPM{' '}
          <NumInput
            value={draft.bpm}
            digits={2}
            onCommit={(v) =>
              v >= 60 && v <= 300 && updateGrid({ ...draft, bpm: v, beat_offset_sec: fold(draft.beat_offset_sec) })
            }
          />
        </label>
        <button onClick={() => draft.bpm / 2 >= 60 && updateGrid({ ...draft, bpm: draft.bpm / 2 })}>÷2</button>
        <button onClick={() => draft.bpm * 2 <= 300 && updateGrid({ ...draft, bpm: draft.bpm * 2 })}>×2</button>
        <label>
          anchor{' '}
          <NumInput
            value={draft.beat_offset_sec}
            digits={3}
            onCommit={(v) => updateGrid({ ...draft, beat_offset_sec: fold(v) })}
          />
          s
        </label>
        {[-10, -1, +1, +10].map((ms) => (
          <button
            key={ms}
            onClick={() => updateGrid({ ...draft, beat_offset_sec: fold(draft.beat_offset_sec + ms / 1000) })}
          >
            {ms > 0 ? `+${ms}` : ms} ms
          </button>
        ))}
        <button onClick={() => updateGrid({ ...draft, beat_offset_sec: fold(draft.beat_offset_sec + beat / 2) })}>
          +½ beat
        </button>
        <label>
          zoom{' '}
          <select value={viewLen} onChange={(e) => setViewLen(Number(e.target.value))}>
            {ZOOMS.map((z) => (
              <option key={z} value={z}>
                {z} s
              </option>
            ))}
          </select>
        </label>
        <button onClick={() => void save()} disabled={!dirty}>
          Save corrections
        </button>
        <button onClick={() => updateGrid(draftOf(analysis))} disabled={!dirty}>
          Revert
        </button>
        {status && <span className="status">{status}</span>}
      </div>

      <OverviewCanvas
        wave={wave}
        sections={draft.sections}
        viewStart={viewStart}
        viewLen={viewLen}
        playheadSec={playhead}
        onSeek={(sec) => setViewStart(clampView(sec - viewLen / 2))}
      />
      <DetailCanvas
        wave={wave}
        bpm={draft.bpm}
        beatOffsetSec={draft.beat_offset_sec}
        viewStart={viewStart}
        viewLen={viewLen}
        playheadSec={playhead}
        onPan={(sec) => setViewStart(clampView(sec))}
      />
      <p className="muted hint">
        Big lines are bars (4 beats), thin lines beats. ▶ Grid check plays the track with a
        metronome on the grid — nudge the anchor (or fix the BPM) until clicks sit on the kicks;
        tweaks apply live while playing. Click the overview to move the view, drag the zoomed
        waveform to pan.
      </p>

      <SectionTable
        sections={draft.sections}
        duration={dur}
        onChange={(sections) => setDraft({ ...draft, sections })}
      />
    </div>
  )
}

function OverviewCanvas({
  wave,
  sections,
  viewStart,
  viewLen,
  playheadSec,
  onSeek,
}: {
  wave: Waveform
  sections: Section[]
  viewStart: number
  viewLen: number
  playheadSec: number | null
  onSeek: (sec: number) => void
}) {
  const [containerRef, width] = useElementWidth<HTMLDivElement>()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const dragging = useRef(false)
  const w = Math.max(width, 100)
  const dur = wave.duration_sec
  const xOf = (sec: number) => (sec / dur) * w

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || w < 10) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.round(w * dpr)
    canvas.height = OVERVIEW_H * dpr
    const ctx = canvas.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.fillStyle = '#131519'
    ctx.fillRect(0, 0, w, OVERVIEW_H)

    const cy = STRIP + (OVERVIEW_H - STRIP) / 2
    ctx.fillStyle = IN_COLOR
    ctx.globalAlpha = 0.8
    for (let x = 0; x < w; x++) {
      const t = ((x + 0.5) / w) * dur
      const peak = wave.peaks[Math.min(Math.floor(t / wave.bin_sec), wave.peaks.length - 1)]
      const h = Math.max(peak * ((OVERVIEW_H - STRIP) / 2 - 2), 0.5)
      ctx.fillRect(x, cy - h, 1, 2 * h)
    }
    ctx.globalAlpha = 1

    for (const s of sections) {
      ctx.fillStyle = SECTION_COLORS[s.label] ?? '#666'
      ctx.globalAlpha = 0.55
      ctx.fillRect(xOf(s.start_sec), 0, xOf(s.end_sec) - xOf(s.start_sec), STRIP)
      ctx.globalAlpha = 1
    }

    ctx.strokeStyle = 'rgba(255,255,255,0.7)'
    ctx.strokeRect(xOf(viewStart), STRIP + 0.5, xOf(viewLen) - xOf(0), OVERVIEW_H - STRIP - 1)

    if (playheadSec != null && playheadSec <= dur) {
      ctx.strokeStyle = 'rgba(255,255,255,0.6)'
      ctx.beginPath()
      ctx.moveTo(xOf(playheadSec), STRIP)
      ctx.lineTo(xOf(playheadSec), OVERVIEW_H)
      ctx.stroke()
    }
  })

  const secAt = (e: React.PointerEvent) => {
    const rect = canvasRef.current!.getBoundingClientRect()
    return ((e.clientX - rect.left) / rect.width) * dur
  }
  return (
    <div className="overview-strip" ref={containerRef}>
      <div className="lane-label">
        <span>full track — click to move the view</span>
      </div>
      <canvas
        ref={canvasRef}
        style={{ height: OVERVIEW_H }}
        onPointerDown={(e) => {
          dragging.current = true
          canvasRef.current!.setPointerCapture(e.pointerId)
          onSeek(secAt(e))
        }}
        onPointerMove={(e) => dragging.current && onSeek(secAt(e))}
        onPointerUp={() => (dragging.current = false)}
      />
    </div>
  )
}

function DetailCanvas({
  wave,
  bpm,
  beatOffsetSec,
  viewStart,
  viewLen,
  playheadSec,
  onPan,
}: {
  wave: Waveform
  bpm: number
  beatOffsetSec: number
  viewStart: number
  viewLen: number
  playheadSec: number | null
  onPan: (viewStartSec: number) => void
}) {
  const [containerRef, width] = useElementWidth<HTMLDivElement>()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const drag = useRef<{ x: number; start: number } | null>(null)
  const w = Math.max(width, 100)
  const beat = 60 / bpm
  const xOf = (sec: number) => ((sec - viewStart) / viewLen) * w

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || w < 10) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.round(w * dpr)
    canvas.height = DETAIL_H * dpr
    const ctx = canvas.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.fillStyle = '#131519'
    ctx.fillRect(0, 0, w, DETAIL_H)

    const cy = DETAIL_H / 2
    ctx.fillStyle = IN_COLOR
    ctx.globalAlpha = 0.85
    for (let x = 0; x < w; x++) {
      const t = viewStart + ((x + 0.5) / w) * viewLen
      if (t < 0 || t > wave.duration_sec) continue
      const peak = wave.peaks[Math.min(Math.floor(t / wave.bin_sec), wave.peaks.length - 1)]
      const h = Math.max(peak * (cy - 3), 0.5)
      ctx.fillRect(x, cy - h, 1, 2 * h)
    }
    ctx.globalAlpha = 1

    // Beat grid: bars full-height and bright, beats short and dim. Hide
    // plain beats when they'd be closer than ~8 px.
    const beatsVisible = viewLen / beat
    const showBeats = w / beatsVisible >= 8
    let n = Math.ceil((viewStart - beatOffsetSec) / beat)
    for (; beatOffsetSec + n * beat <= viewStart + viewLen; n++) {
      const x = xOf(beatOffsetSec + n * beat)
      const bar = ((n % 4) + 4) % 4 === 0
      if (!bar && !showBeats) continue
      ctx.strokeStyle = bar ? 'rgba(255,255,255,0.8)' : 'rgba(255,255,255,0.25)'
      ctx.lineWidth = bar ? 1.5 : 1
      ctx.beginPath()
      ctx.moveTo(x, bar ? 0 : DETAIL_H * 0.2)
      ctx.lineTo(x, bar ? DETAIL_H : DETAIL_H * 0.8)
      ctx.stroke()
    }

    if (playheadSec != null && playheadSec >= viewStart && playheadSec <= viewStart + viewLen) {
      ctx.strokeStyle = '#ffd24a'
      ctx.lineWidth = 1.5
      ctx.beginPath()
      ctx.moveTo(xOf(playheadSec), 0)
      ctx.lineTo(xOf(playheadSec), DETAIL_H)
      ctx.stroke()
    }
  })

  return (
    <div className="overview-strip" ref={containerRef}>
      <div className="lane-label">
        <span>beat grid — drag to pan</span>
        <span className="muted">
          {viewStart.toFixed(1)}–{(viewStart + viewLen).toFixed(1)} s
        </span>
      </div>
      <canvas
        ref={canvasRef}
        style={{ height: DETAIL_H, cursor: 'grab' }}
        onPointerDown={(e) => {
          drag.current = { x: e.clientX, start: viewStart }
          canvasRef.current!.setPointerCapture(e.pointerId)
        }}
        onPointerMove={(e) => {
          if (!drag.current) return
          const rect = canvasRef.current!.getBoundingClientRect()
          const dt = ((drag.current.x - e.clientX) / rect.width) * viewLen
          onPan(drag.current.start + dt)
        }}
        onPointerUp={() => (drag.current = null)}
      />
    </div>
  )
}

function SectionTable({
  sections,
  duration,
  onChange,
}: {
  sections: Section[]
  duration: number
  onChange: (sections: Section[]) => void
}) {
  const update = (i: number, patch: Partial<Section>) =>
    onChange(sections.map((s, j) => (j === i ? { ...s, ...patch } : s)))

  return (
    <div className="section-table">
      <div className="lane-label">
        <span>sections (feed the seam suggester)</span>
      </div>
      <table>
        <tbody>
          {sections.map((s, i) => (
            <tr key={i}>
              <td>
                <span className="section-dot" style={{ background: SECTION_COLORS[s.label] ?? '#666' }} />
                <select value={s.label} onChange={(e) => update(i, { label: e.target.value })}>
                  {SECTION_LABELS.map((l) => (
                    <option key={l} value={l}>
                      {l}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <TimeInput
                  valueSec={s.start_sec}
                  onCommit={(sec) => update(i, { start_sec: Math.min(Math.max(sec, 0), s.end_sec - 0.1) })}
                />
                {' – '}
                <TimeInput
                  valueSec={s.end_sec}
                  onCommit={(sec) => update(i, { end_sec: Math.max(Math.min(sec, duration), s.start_sec + 0.1) })}
                />
              </td>
              <td className="row-actions">
                <button
                  title="split in the middle"
                  onClick={() => {
                    const mid = (s.start_sec + s.end_sec) / 2
                    onChange([
                      ...sections.slice(0, i),
                      { ...s, end_sec: mid },
                      { ...s, start_sec: mid },
                      ...sections.slice(i + 1),
                    ])
                  }}
                >
                  split
                </button>
                <button onClick={() => onChange(sections.filter((_, j) => j !== i))}>delete</button>
              </td>
            </tr>
          ))}
          {sections.length === 0 && (
            <tr>
              <td className="muted">no sections</td>
            </tr>
          )}
        </tbody>
      </table>
      <button
        onClick={() => {
          const start = sections.length ? sections[sections.length - 1].end_sec : 0
          onChange([...sections, { label: 'drop', start_sec: start, end_sec: Math.min(start + 30, duration) }])
        }}
      >
        + add section
      </button>
    </div>
  )
}

/** Numeric field that commits on blur/Enter and reverts bad input. */
function NumInput({
  value,
  digits,
  onCommit,
}: {
  value: number
  digits: number
  onCommit: (v: number) => void
}) {
  const [text, setText] = useState(value.toFixed(digits))
  useEffect(() => {
    setText(value.toFixed(digits))
  }, [value, digits])
  return (
    <input
      className="time-input"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        const v = Number(text)
        if (Number.isFinite(v)) onCommit(v)
        else setText(value.toFixed(digits))
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
      }}
    />
  )
}
