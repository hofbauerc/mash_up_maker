// Grid-check playback for the track inspector: the original track plus a
// metronome click on every grid beat (accented on bars), scheduled sample-
// accurately on one AudioContext so a correct grid is instantly audible —
// and a wrong BPM/offset drifts against the kicks within a few bars.

import { api } from '../api/client'

const SCHED_INTERVAL_MS = 400
const SCHED_HORIZON_SEC = 1.2

export class GridCheckPlayer {
  private ctx: AudioContext | null = null
  private buf: AudioBuffer | null = null
  private bufTrackId = -1
  private src: AudioBufferSourceNode | null = null
  private clickBus: GainNode | null = null
  private timer = 0
  private startedAt = 0
  private startOffset = 0
  playing = false

  get duration(): number | null {
    return this.buf?.duration ?? null
  }

  /** Decode the track's original file (cached per track id). */
  async load(trackId: number): Promise<void> {
    this.ctx ??= new AudioContext()
    if (this.bufTrackId === trackId && this.buf) return
    const res = await fetch(api.trackAudioUrl(trackId))
    if (!res.ok) throw new Error(`${res.status} fetching track audio`)
    this.buf = await this.ctx.decodeAudioData(await res.arrayBuffer())
    this.bufTrackId = trackId
  }

  /** Current playback position in track time, seconds. */
  position(): number {
    if (!this.playing || !this.ctx) return 0
    return this.startOffset + this.ctx.currentTime - this.startedAt
  }

  play(fromSec: number, bpm: number, beatOffsetSec: number): void {
    if (!this.ctx || !this.buf) return
    this.stop()
    void this.ctx.resume()
    const ctx = this.ctx
    const t0 = ctx.currentTime + 0.08
    const from = Math.min(Math.max(fromSec, 0), this.buf.duration - 0.1)

    const master = ctx.createGain()
    master.gain.value = 0.85
    master.connect(ctx.destination)
    this.src = ctx.createBufferSource()
    this.src.buffer = this.buf
    this.src.connect(master)
    this.src.start(t0, from)

    this.clickBus = ctx.createGain()
    this.clickBus.gain.value = 1.0
    this.clickBus.connect(ctx.destination)

    this.startedAt = t0
    this.startOffset = from
    this.playing = true

    // Rolling click scheduler: beat n sits at beatOffsetSec + n * beat.
    const beat = 60 / bpm
    let nextBeat = Math.ceil((from - beatOffsetSec) / beat - 1e-6)
    const scheduleAhead = () => {
      const horizon = ctx.currentTime + SCHED_HORIZON_SEC
      for (;;) {
        const trackT = beatOffsetSec + nextBeat * beat
        const ctxT = this.startedAt + (trackT - this.startOffset)
        if (ctxT > horizon) break
        if (trackT > this.buf!.duration) break
        if (ctxT >= ctx.currentTime) this.click(ctxT, nextBeat % 4 === 0)
        nextBeat++
      }
    }
    scheduleAhead()
    this.timer = window.setInterval(scheduleAhead, SCHED_INTERVAL_MS)
  }

  private click(when: number, accent: boolean): void {
    const ctx = this.ctx!
    const osc = ctx.createOscillator()
    osc.frequency.value = accent ? 1760 : 1175
    const env = ctx.createGain()
    env.gain.setValueAtTime(accent ? 0.5 : 0.3, when)
    env.gain.exponentialRampToValueAtTime(0.001, when + 0.03)
    osc.connect(env)
    env.connect(this.clickBus!)
    osc.start(when)
    osc.stop(when + 0.04)
  }

  stop(): void {
    window.clearInterval(this.timer)
    try {
      this.src?.stop()
    } catch {
      /* not started yet */
    }
    this.src?.disconnect()
    this.clickBus?.disconnect()
    this.src = null
    this.clickBus = null
    this.playing = false
  }

  async close(): Promise<void> {
    this.stop()
    await this.ctx?.close()
    this.ctx = null
  }
}
