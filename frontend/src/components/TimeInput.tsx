import { useEffect, useState } from 'react'

/** m:ss.s time field that commits on blur/Enter and reverts bad input. */
export function TimeInput({ valueSec, onCommit }: { valueSec: number; onCommit: (sec: number) => void }) {
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

export function parseTime(text: string): number | null {
  const t = text.trim()
  const m = /^(\d+):(\d{1,2}(?:\.\d+)?)$/.exec(t)
  if (m) return Number(m[1]) * 60 + Number(m[2])
  if (/^\d+(\.\d+)?$/.test(t)) return Number(t)
  return null
}

export function fmtTime(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = sec - m * 60
  return `${m}:${s.toFixed(1).padStart(4, '0')}`
}
