import { useRef } from 'react'
import type { CurvePoint } from '../types'
import { useElementWidth } from './useElementWidth'

const H = 64
const PAD_X = 8
const PAD_Y = 6

interface CurveLaneProps {
  label: string
  points: CurvePoint[]
  /** Curve domain: beats 0..blendBeats within the side's transition window. */
  blendBeats: number
  min: number
  max: number
  /** Value the lane means when it has no points (drawn as a dashed line). */
  defaultValue: number
  /** Logarithmic value axis (filter cutoffs). */
  log?: boolean
  format: (v: number) => string
  onChange: (points: CurvePoint[], commit: boolean) => void
}

// Click to add a point, drag to move it, double-click (or right-click) to
// remove. Beats snap to the grid; a point's beat stays between its neighbors
// so indices are stable while dragging.
export function CurveLane({
  label,
  points,
  blendBeats,
  min,
  max,
  defaultValue,
  log,
  format,
  onChange,
}: CurveLaneProps) {
  const [containerRef, width] = useElementWidth<HTMLDivElement>()
  const svgRef = useRef<SVGSVGElement>(null)
  const dragIdx = useRef<number | null>(null)

  const w = Math.max(width, 80)
  const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi)

  const xOf = (beat: number) => PAD_X + (clamp(beat, 0, blendBeats) / blendBeats) * (w - 2 * PAD_X)
  const frac = (v: number) =>
    log
      ? (Math.log(v) - Math.log(min)) / (Math.log(max) - Math.log(min))
      : (v - min) / (max - min)
  const yOf = (v: number) => H - PAD_Y - frac(clamp(v, min, max)) * (H - 2 * PAD_Y)

  const beatAt = (x: number) => clamp(Math.round(((x - PAD_X) / (w - 2 * PAD_X)) * blendBeats), 0, blendBeats)
  const valueAt = (y: number) => {
    const t = clamp((H - PAD_Y - y) / (H - 2 * PAD_Y), 0, 1)
    const v = log ? Math.exp(Math.log(min) + t * (Math.log(max) - Math.log(min))) : min + t * (max - min)
    return Math.round(v * 1000) / 1000
  }

  const pos = (e: React.PointerEvent | React.MouseEvent) => {
    const rect = svgRef.current!.getBoundingClientRect()
    return { x: e.clientX - rect.left, y: e.clientY - rect.top }
  }

  const hitPoint = (x: number, y: number) => {
    let best = -1
    let bestDist = 10
    points.forEach((p, i) => {
      const d = Math.hypot(xOf(p.beat) - x, yOf(p.value) - y)
      if (d < bestDist) {
        best = i
        bestDist = d
      }
    })
    return best
  }

  function onPointerDown(e: React.PointerEvent<SVGSVGElement>) {
    const { x, y } = pos(e)
    let idx = hitPoint(x, y)
    if (idx < 0) {
      const point = { beat: beatAt(x), value: valueAt(y) }
      const next = [...points, point].sort((a, b) => a.beat - b.beat)
      idx = next.indexOf(point)
      onChange(next, false)
    }
    dragIdx.current = idx
    svgRef.current!.setPointerCapture(e.pointerId)
  }

  function onPointerMove(e: React.PointerEvent<SVGSVGElement>) {
    const idx = dragIdx.current
    if (idx === null || idx >= points.length) return
    const { x, y } = pos(e)
    const lo = idx > 0 ? points[idx - 1].beat : 0
    const hi = idx < points.length - 1 ? points[idx + 1].beat : blendBeats
    const next = [...points]
    next[idx] = { beat: clamp(beatAt(x), lo, hi), value: valueAt(y) }
    onChange(next, false)
  }

  function onPointerUp() {
    if (dragIdx.current === null) return
    dragIdx.current = null
    onChange(points, true)
  }

  function removeAt(e: React.MouseEvent<SVGSVGElement>) {
    e.preventDefault()
    const { x, y } = pos(e)
    const idx = hitPoint(x, y)
    if (idx >= 0) onChange(points.filter((_, i) => i !== idx), true)
  }

  const gridBeats: number[] = []
  for (let b = 0; b <= blendBeats; b += 4) gridBeats.push(b)

  const line =
    points.length > 0
      ? `M 0 ${yOf(points[0].value)} ` +
        points.map((p) => `L ${xOf(p.beat)} ${yOf(p.value)}`).join(' ') +
        ` L ${w} ${yOf(points[points.length - 1].value)}`
      : null

  return (
    <div className="curve-lane" ref={containerRef}>
      <div className="lane-label">
        <span>{label}</span>
        <span className="muted">{points.length === 0 ? `default ${format(defaultValue)}` : ''}</span>
      </div>
      <svg
        ref={svgRef}
        height={H}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onDoubleClick={removeAt}
        onContextMenu={removeAt}
      >
        {gridBeats.map((b) => (
          <line
            key={b}
            x1={xOf(b)}
            y1={0}
            x2={xOf(b)}
            y2={H}
            stroke="#2a2d34"
            strokeWidth={b % 16 === 0 ? 1.5 : 0.5}
          />
        ))}
        <line
          x1={0}
          y1={yOf(defaultValue)}
          x2={w}
          y2={yOf(defaultValue)}
          stroke="#555"
          strokeDasharray="4 4"
        />
        {line && <path d={line} fill="none" stroke="var(--accent)" strokeWidth={1.5} />}
        {points.map((p, i) => (
          <circle key={i} cx={xOf(p.beat)} cy={yOf(p.value)} r={4.5} fill="var(--accent)">
            <title>{`beat ${p.beat} · ${format(p.value)}`}</title>
          </circle>
        ))}
      </svg>
    </div>
  )
}
