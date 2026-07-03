import { useEffect, useRef } from 'react'
import type { Analysis, SeamParams, Waveform } from '../types'
import { IN_COLOR, OUT_COLOR, SECTION_COLORS } from './palette'
import { useElementWidth } from './useElementWidth'

// Overlapped, beat-aligned waveform view of one seam (DESIGN.md #7).
//
// The x axis is *outgoing-track time* τ. The transition window is the
// `blend_beats` ending at the exit point; the incoming track enters at the
// window start (blend) or at the exit point (cut) and is drawn on the lower
// lane in its mix-aligned position. Drag interactions:
//   - exit line        → out_point_sec (snaps to the outgoing beat grid)
//   - window start     → blend_beats  (snaps to 4-beat steps)
//   - lower lane       → in_point_sec (snaps to the incoming beat grid)
// The view is frozen while dragging so handles follow the pointer instead of
// the content sliding underneath.

const H = 240
const STRIP = 7
const OUT_LANE = { top: 12, h: 100 }
const IN_LANE = { top: 126, h: 100 }
const VIEW_PAD_BEATS = 16

interface SeamWaveformProps {
  outWave: Waveform
  inWave: Waveform
  outAnalysis: Analysis
  inAnalysis: Analysis
  params: SeamParams
  /** Preview playhead position in outgoing-track time, if playing. */
  playheadTau?: number | null
  onChange: (params: SeamParams, commit: boolean) => void
}

interface Geometry {
  beatOut: number
  beatIn: number
  windowSec: number
  outPoint: number
  winStart: number
  entryTau: number
}

type DragMode = 'exit' | 'blend' | 'in'

export function SeamWaveform({
  outWave,
  inWave,
  outAnalysis,
  inAnalysis,
  params,
  playheadTau,
  onChange,
}: SeamWaveformProps) {
  const [containerRef, width] = useElementWidth<HTMLDivElement>()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const drag = useRef<{ mode: DragMode; view: [number, number]; startX: number; startInPoint: number } | null>(null)

  const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)
  const round3 = (v: number) => Math.round(v * 1000) / 1000

  function geometry(): Geometry {
    const beatOut = 60 / outAnalysis.bpm
    const beatIn = 60 / inAnalysis.bpm
    const windowSec = params.blend_beats * beatOut
    const outPoint = params.out_point_sec ?? outWave.duration_sec
    const winStart = outPoint - windowSec
    const entryTau = params.template === 'blend' ? winStart : outPoint
    return { beatOut, beatIn, windowSec, outPoint, winStart, entryTau }
  }

  function idleView(g: Geometry): [number, number] {
    return [g.winStart - VIEW_PAD_BEATS * g.beatOut, g.outPoint + VIEW_PAD_BEATS * g.beatOut]
  }

  const w = Math.max(width, 200)
  const xOf = (tau: number, view: [number, number]) => ((tau - view[0]) / (view[1] - view[0])) * w
  const tauAt = (x: number, view: [number, number]) => view[0] + (x / w) * (view[1] - view[0])

  const snapOut = (t: number, g: Geometry) =>
    outAnalysis.beat_offset_sec + Math.round((t - outAnalysis.beat_offset_sec) / g.beatOut) * g.beatOut
  const snapIn = (t: number, g: Geometry) =>
    inAnalysis.beat_offset_sec + Math.round((t - inAnalysis.beat_offset_sec) / g.beatIn) * g.beatIn

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || w < 10) return
    const g = geometry()
    const view = drag.current?.view ?? idleView(g)
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.round(w * dpr)
    canvas.height = H * dpr
    const ctx = canvas.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    ctx.fillStyle = '#131519'
    ctx.fillRect(0, 0, w, H)

    // Transition window band.
    const wx0 = xOf(g.winStart, view)
    const wx1 = xOf(g.outPoint, view)
    ctx.fillStyle = 'rgba(255, 90, 54, 0.07)'
    ctx.fillRect(wx0, 0, wx1 - wx0, H)

    drawSections(ctx, outAnalysis, view, (t) => t, 0)
    drawSections(ctx, inAnalysis, view, (inT) => g.entryTau + (inT - params.in_point_sec), H - STRIP)

    // Outgoing waveform: dimmed after the exit point (that material is cut).
    drawWave(ctx, outWave, view, (tau) => tau, OUT_LANE, OUT_COLOR, (tau) => (tau > g.outPoint ? 0.25 : 0.95))
    // Incoming waveform: ghosted before its entry (material that won't play).
    drawWave(ctx, inWave, view, (tau) => params.in_point_sec + (tau - g.entryTau), IN_LANE, IN_COLOR, (tau) =>
      tau < g.entryTau ? 0.22 : 0.95,
    )

    // Grids go over the waveforms — hard-dance masters are a solid block at
    // this zoom and would hide them entirely otherwise.
    drawBeatGrid(ctx, view, outAnalysis.beat_offset_sec, g.beatOut, (t) => t, OUT_LANE)
    drawBeatGrid(ctx, view, inAnalysis.beat_offset_sec, g.beatIn, (inT) => g.entryTau + (inT - params.in_point_sec), IN_LANE)

    // Window-start handle (dashed) and exit handle (solid).
    ctx.strokeStyle = OUT_COLOR
    ctx.setLineDash([5, 4])
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(wx0, 0)
    ctx.lineTo(wx0, H)
    ctx.stroke()
    ctx.setLineDash([])
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.moveTo(wx1, 0)
    ctx.lineTo(wx1, H)
    ctx.stroke()
    ctx.fillStyle = OUT_COLOR
    ctx.fillRect(wx1 - 5, OUT_LANE.top - 10, 10, 10)
    ctx.fillRect(wx0 - 5, OUT_LANE.top + OUT_LANE.h, 10, 10)

    ctx.font = '10px sans-serif'
    ctx.fillStyle = '#c9c9c9'
    ctx.fillText('exit', wx1 + 5, OUT_LANE.top - 2)
    ctx.fillText(`${params.blend_beats}-beat window`, wx0 + 5, OUT_LANE.top + OUT_LANE.h + 9)
    ctx.fillStyle = IN_COLOR
    ctx.fillText('in ▸', xOf(g.entryTau, view) + 5, IN_LANE.top + 11)

    if (playheadTau != null) {
      const px = xOf(playheadTau, view)
      if (px >= 0 && px <= w) {
        ctx.strokeStyle = '#ffffff'
        ctx.lineWidth = 1.5
        ctx.beginPath()
        ctx.moveTo(px, 0)
        ctx.lineTo(px, H)
        ctx.stroke()
      }
    }
  })

  function drawSections(
    ctx: CanvasRenderingContext2D,
    analysis: Analysis,
    view: [number, number],
    toTau: (trackT: number) => number,
    y: number,
  ) {
    for (const s of analysis.sections) {
      const x0 = clamp(xOf(toTau(s.start_sec), view), 0, w)
      const x1 = clamp(xOf(toTau(s.end_sec), view), 0, w)
      if (x1 <= x0) continue
      ctx.fillStyle = SECTION_COLORS[s.label] ?? '#666'
      ctx.globalAlpha = 0.55
      ctx.fillRect(x0, y, x1 - x0, STRIP)
      ctx.globalAlpha = 1
    }
  }

  function drawBeatGrid(
    ctx: CanvasRenderingContext2D,
    view: [number, number],
    offset: number,
    beatLen: number,
    toTau: (trackT: number) => number,
    lane: { top: number; h: number },
  ) {
    // Visible beat indices: invert toTau (it's a pure shift) via its value at 0.
    const shift = toTau(0)
    const nFrom = Math.ceil((view[0] - shift - offset) / beatLen)
    const nTo = Math.floor((view[1] - shift - offset) / beatLen)
    for (let n = Math.max(0, nFrom); n <= nTo; n++) {
      if (n % 4 !== 0) continue
      const alpha = n % 32 === 0 ? 0.4 : n % 16 === 0 ? 0.25 : 0.12
      const x = xOf(offset + n * beatLen + shift, view)
      ctx.strokeStyle = `rgba(255,255,255,${alpha})`
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(x, lane.top)
      ctx.lineTo(x, lane.top + lane.h)
      ctx.stroke()
    }
  }

  function drawWave(
    ctx: CanvasRenderingContext2D,
    wave: Waveform,
    view: [number, number],
    toTrackT: (tau: number) => number,
    lane: { top: number; h: number },
    color: string,
    alphaAt: (tau: number) => number,
  ) {
    const cy = lane.top + lane.h / 2
    ctx.fillStyle = color
    for (let x = 0; x < w; x++) {
      const tau = tauAt(x + 0.5, view)
      const t = toTrackT(tau)
      if (t < 0 || t >= wave.duration_sec) continue
      const peak = wave.peaks[Math.min(Math.floor(t / wave.bin_sec), wave.peaks.length - 1)]
      const h = Math.max(peak * (lane.h / 2 - 3), 0.6)
      ctx.globalAlpha = alphaAt(tau)
      ctx.fillRect(x, cy - h, 1, 2 * h)
    }
    ctx.globalAlpha = 1
  }

  function hitTest(x: number, y: number, g: Geometry, view: [number, number]): DragMode | null {
    if (Math.abs(x - xOf(g.outPoint, view)) <= 7) return 'exit'
    if (Math.abs(x - xOf(g.winStart, view)) <= 7) return 'blend'
    if (y >= IN_LANE.top && y <= IN_LANE.top + IN_LANE.h) return 'in'
    return null
  }

  const pos = (e: React.PointerEvent) => {
    const rect = canvasRef.current!.getBoundingClientRect()
    return { x: e.clientX - rect.left, y: e.clientY - rect.top }
  }

  function onPointerDown(e: React.PointerEvent<HTMLCanvasElement>) {
    const g = geometry()
    const view = idleView(g)
    const { x, y } = pos(e)
    const mode = hitTest(x, y, g, view)
    if (!mode) return
    drag.current = { mode, view, startX: x, startInPoint: params.in_point_sec }
    canvasRef.current!.setPointerCapture(e.pointerId)
  }

  function onPointerMove(e: React.PointerEvent<HTMLCanvasElement>) {
    const g = geometry()
    const { x, y } = pos(e)
    if (!drag.current) {
      const mode = hitTest(x, y, g, idleView(g))
      canvasRef.current!.style.cursor = mode ? 'ew-resize' : 'default'
      return
    }
    const { mode, view, startX, startInPoint } = drag.current
    if (mode === 'exit') {
      const t = round3(clamp(snapOut(tauAt(x, view), g), 4 * g.beatOut, outWave.duration_sec))
      if (t !== params.out_point_sec) onChange({ ...params, out_point_sec: t }, false)
    } else if (mode === 'blend') {
      const rawBeats = Math.round((g.outPoint - tauAt(x, view)) / g.beatOut / 4) * 4
      const maxBeats = Math.max(4, Math.floor(g.outPoint / g.beatOut / 4) * 4)
      const beats = clamp(rawBeats, 4, Math.min(128, maxBeats))
      if (beats !== params.blend_beats) onChange({ ...params, blend_beats: beats }, false)
    } else {
      const dTau = ((x - startX) / w) * (view[1] - view[0])
      const t = round3(clamp(snapIn(startInPoint - dTau, g), 0, Math.max(0, inWave.duration_sec - 1)))
      if (t !== params.in_point_sec) onChange({ ...params, in_point_sec: t }, false)
    }
  }

  function onPointerUp() {
    if (!drag.current) return
    drag.current = null
    onChange(params, true)
  }

  return (
    <div ref={containerRef}>
      <canvas
        ref={canvasRef}
        className="seam-waveform"
        style={{ height: H }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      />
    </div>
  )
}
