import { useEffect, useRef } from 'react'
import type { Analysis, Waveform } from '../types'
import { drawSpectralColumn, SECTION_COLORS } from './palette'
import { useElementWidth } from './useElementWidth'

// Full-track overview: click or drag anywhere to place the seam point there
// (snapped to 4-beat bars). Made for the big jumps the detail view is too
// zoomed-in for — e.g. pulling the exit out of the outro back to the last
// kick section — with fine-tuning left to the detail waveform below.

const H = 46
const STRIP = 5

interface OverviewStripProps {
  label: string
  wave: Waveform
  analysis: Analysis
  /** The seam point this strip edits (exit or entry), in track time. */
  markerSec: number
  /** Transition window length in seconds, shaded next to the marker. */
  windowSec: number
  /** Which side of the marker the window lies on. */
  windowSide: 'before' | 'after'
  color: string
  /** [low, mid, high] shades for the spectral view (falls back to `color`). */
  bandColors?: [string, string, string]
  playheadSec?: number | null
  onChange: (sec: number, commit: boolean) => void
}

export function OverviewStrip({
  label,
  wave,
  analysis,
  markerSec,
  windowSec,
  windowSide,
  color,
  bandColors,
  playheadSec,
  onChange,
}: OverviewStripProps) {
  const [containerRef, width] = useElementWidth<HTMLDivElement>()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const dragging = useRef(false)

  const w = Math.max(width, 100)
  const dur = wave.duration_sec
  const beat = 60 / analysis.bpm
  const xOf = (sec: number) => (sec / dur) * w

  const snap = (sec: number) => {
    const bar = 4 * beat
    const snapped = analysis.beat_offset_sec + Math.round((sec - analysis.beat_offset_sec) / bar) * bar
    const lo = windowSide === 'before' ? bar : 0
    return Math.round(Math.min(Math.max(snapped, lo), dur) * 1000) / 1000
  }

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || w < 10) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.round(w * dpr)
    canvas.height = H * dpr
    const ctx = canvas.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    ctx.fillStyle = '#131519'
    ctx.fillRect(0, 0, w, H)

    const cy = STRIP + (H - STRIP) / 2
    ctx.globalAlpha = 0.8
    for (let x = 0; x < w; x++) {
      const t = ((x + 0.5) / w) * dur
      const bin = Math.min(Math.floor(t / wave.bin_sec), wave.peaks.length - 1)
      const h = Math.max(wave.peaks[bin] * ((H - STRIP) / 2 - 2), 0.5)
      const band = bandColors ? wave.bands?.[bin] : undefined
      if (band) {
        drawSpectralColumn(ctx, x, cy, h, band, bandColors!)
      } else {
        ctx.fillStyle = color
        ctx.fillRect(x, cy - h, 1, 2 * h)
      }
    }
    ctx.globalAlpha = 1

    for (const s of analysis.sections) {
      ctx.fillStyle = SECTION_COLORS[s.label] ?? '#666'
      ctx.globalAlpha = 0.55
      ctx.fillRect(xOf(s.start_sec), 0, xOf(s.end_sec) - xOf(s.start_sec), STRIP)
      ctx.globalAlpha = 1
    }

    const w0 = windowSide === 'before' ? markerSec - windowSec : markerSec
    ctx.fillStyle = 'rgba(255,255,255,0.14)'
    ctx.fillRect(xOf(w0), STRIP, xOf(windowSec) - xOf(0), H - STRIP)

    const mx = xOf(markerSec)
    ctx.strokeStyle = '#ffffff'
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.moveTo(mx, 0)
    ctx.lineTo(mx, H)
    ctx.stroke()

    if (playheadSec != null && playheadSec >= 0 && playheadSec <= dur) {
      ctx.strokeStyle = 'rgba(255,255,255,0.6)'
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(xOf(playheadSec), STRIP)
      ctx.lineTo(xOf(playheadSec), H)
      ctx.stroke()
    }
  })

  const secAt = (e: React.PointerEvent) => {
    const rect = canvasRef.current!.getBoundingClientRect()
    return ((e.clientX - rect.left) / rect.width) * dur
  }

  function onPointerDown(e: React.PointerEvent<HTMLCanvasElement>) {
    dragging.current = true
    canvasRef.current!.setPointerCapture(e.pointerId)
    onChange(snap(secAt(e)), false)
  }

  function onPointerMove(e: React.PointerEvent<HTMLCanvasElement>) {
    if (!dragging.current) return
    const sec = snap(secAt(e))
    if (sec !== markerSec) onChange(sec, false)
  }

  function onPointerUp(e: React.PointerEvent<HTMLCanvasElement>) {
    if (!dragging.current) return
    dragging.current = false
    onChange(snap(secAt(e)), true)
  }

  return (
    <div className="overview-strip" ref={containerRef}>
      <div className="lane-label">
        <span>{label}</span>
        <span className="muted">bar-snapped</span>
      </div>
      <canvas
        ref={canvasRef}
        style={{ height: H }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      />
    </div>
  )
}
