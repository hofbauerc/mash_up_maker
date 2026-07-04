// Adjacency compatibility for seam warning badges (Phase 1.5).
//
// Mirrors backend/app/ordering.py: camelot_distance and the BPM/key/energy
// dimensions of its cost function, evaluated live on the *current* (possibly
// hand-reordered) set so badges update as tracks move. Thresholds follow the
// backend's blend/cut rule (seams.py suggests a cut past a 10% BPM gap).

import type { Track } from './types'

export type CompatLevel = 'ok' | 'caution' | 'warn'

export interface CompatBadge {
  text: string
  level: CompatLevel
  title: string
}

/** Steps on the Camelot wheel; 0 = same, 1 = mixable neighbor. */
export function camelotDistance(a: string | null, b: string | null): number | null {
  if (!a || !b) return null
  const na = parseInt(a.slice(0, -1), 10)
  const nb = parseInt(b.slice(0, -1), 10)
  if (!Number.isFinite(na) || !Number.isFinite(nb)) return null
  const diff = (((na - nb) % 12) + 12) % 12
  return Math.min(diff, 12 - diff) + (a.slice(-1) === b.slice(-1) ? 0 : 1)
}

/** Badges for the seam between `out` and `inc`, worst dimensions first. */
export function seamBadges(out: Track, inc: Track): CompatBadge[] {
  const badges: CompatBadge[] = []

  if (out.bpm && inc.bpm) {
    const gapPct = (Math.abs(out.bpm - inc.bpm) / out.bpm) * 100
    const level: CompatLevel = gapPct <= 4 ? 'ok' : gapPct <= 10 ? 'caution' : 'warn'
    badges.push({
      text: `Δ${gapPct.toFixed(1)}% BPM`,
      level,
      title:
        level === 'ok'
          ? 'Close tempos — clean blend.'
          : level === 'caution'
            ? 'Blend works, but the tempo stretch will be noticeable.'
            : 'Blends past a 10% gap stretch audibly — prefer a cut or slam.',
    })
  }

  const keyDist = camelotDistance(out.camelot, inc.camelot)
  if (keyDist !== null) {
    const level: CompatLevel = keyDist <= 1 ? 'ok' : keyDist === 2 ? 'caution' : 'warn'
    badges.push({
      text: `${out.camelot}→${inc.camelot}`,
      level,
      title:
        level === 'ok'
          ? 'Keys match or are Camelot neighbors — harmonic blend.'
          : level === 'caution'
            ? 'Two Camelot steps apart — melodic overlap may clash.'
            : 'Distant keys — avoid overlapping melodic sections; EQ or cut instead.',
    })
  }

  if (out.energy != null && inc.energy != null) {
    const drop = out.energy - inc.energy
    if (drop > 0.15) {
      badges.push({
        text: `energy −${drop.toFixed(2)}`,
        level: drop > 0.3 ? 'warn' : 'caution',
        title: 'The incoming track is noticeably lower-energy — the set loses momentum here.',
      })
    }
  }

  const rank = { warn: 0, caution: 1, ok: 2 }
  return badges.sort((a, b) => rank[a.level] - rank[b.level])
}
