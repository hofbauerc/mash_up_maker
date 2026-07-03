// Hybrid preview engine (DESIGN.md #8).
//
// The backend renders raw tempo-matched segments around one seam
// (POST /api/seams/preview); this engine decodes them and builds a live
// Web Audio graph per side:
//
//   BufferSource -> Gain (volume curve / template fade)
//                -> BiquadFilter x3 (low shelf 200 Hz, peak 1.2 kHz,
//                                    high shelf 6 kHz — mirrors fx.py)
//                -> BiquadFilter (low/high-pass sweep, when active)
//                -> master -> destination
//   outgoing also: post-chain send -> delay (w/ feedback) or convolver
//                  reverb, opened over the last 4 beats before the exit.
//
// Automation curves map to AudioParam ramps, so curve tweaks are audible by
// restarting playback at the current position — no server round-trip. Only
// cut points / window / template / tempo changes need new segments. The
// server render stays the ground truth for export (DESIGN.md risk #1).

import type { CurvePoint, SeamParams, SeamPreviewOut, SideAutomation, TailFX } from '../types'

export interface LoadedPreview {
  meta: SeamPreviewOut
  outBuf: AudioBuffer
  inBuf: AudioBuffer
}

/** Params that require re-rendering server segments (vs. live-applied curves). */
export function segmentSignature(p: SeamParams): string {
  return [p.template, p.out_point_sec, p.in_point_sec, p.blend_beats].join('|')
}

interface TimeValue {
  t: number // preview time, seconds
  v: number
}

export class PreviewEngine {
  private ctx: AudioContext
  private nodes: AudioNode[] = []
  private sources: AudioBufferSourceNode[] = []
  private startedAt = 0
  private startOffset = 0
  playing = false

  constructor() {
    this.ctx = new AudioContext({ sampleRate: 44100 })
  }

  async load(meta: SeamPreviewOut): Promise<LoadedPreview> {
    const [outBuf, inBuf] = await Promise.all([this.fetchBuffer(meta.out_url), this.fetchBuffer(meta.in_url)])
    return { meta, outBuf, inBuf }
  }

  private async fetchBuffer(url: string): Promise<AudioBuffer> {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`${res.status} fetching ${url}`)
    return this.ctx.decodeAudioData(await res.arrayBuffer())
  }

  /** Current playback position in preview time, seconds. */
  position(): number {
    return this.playing ? this.startOffset + this.ctx.currentTime - this.startedAt : 0
  }

  play(pv: LoadedPreview, params: SeamParams, outBpm: number, fromSec = 0): void {
    this.stop()
    void this.ctx.resume()
    const t0 = this.ctx.currentTime + 0.08
    const beat = 60 / outBpm
    const windowSec = pv.meta.window_sec
    const exitSec = pv.outBuf.duration // outgoing segment ends at the exit
    const outWinStart = Math.max(0, exitSec - windowSec)

    const master = this.ctx.createGain()
    master.gain.value = 0.9
    master.connect(this.ctx.destination)
    this.nodes.push(master)

    this.buildSide({
      buf: pv.outBuf,
      bufStartPreview: 0,
      auto: params.out_auto,
      winStart: outWinStart,
      windowSec,
      beat,
      defaultVolume:
        params.template === 'blend'
          ? equalPower(outWinStart, windowSec, 'out')
          : [{ t: 0, v: 1 }],
      tail: params.tail,
      exitSec,
      master,
      t0,
      fromSec,
    })
    this.buildSide({
      buf: pv.inBuf,
      bufStartPreview: pv.meta.entry_sec,
      auto: params.in_auto,
      winStart: pv.meta.entry_sec,
      windowSec,
      beat,
      defaultVolume:
        params.template === 'blend'
          ? equalPower(pv.meta.entry_sec, windowSec, 'in')
          : [{ t: 0, v: 1 }],
      tail: null,
      exitSec,
      master,
      t0,
      fromSec,
    })

    this.startedAt = t0
    this.startOffset = fromSec
    this.playing = true
  }

  private buildSide(o: {
    buf: AudioBuffer
    bufStartPreview: number
    auto: SideAutomation
    winStart: number
    windowSec: number
    beat: number
    defaultVolume: TimeValue[]
    tail: TailFX | null
    exitSec: number
    master: GainNode
    t0: number
    fromSec: number
  }): void {
    const src = this.ctx.createBufferSource()
    src.buffer = o.buf
    const volume = this.ctx.createGain()
    const eqLow = this.biquad('lowshelf', 200)
    const eqMid = this.biquad('peaking', 1200)
    const eqHigh = this.biquad('highshelf', 6000)
    let head: AudioNode = src
    for (const node of [volume, eqLow, eqMid, eqHigh]) {
      head.connect(node)
      head = node
    }
    let sweep: BiquadFilterNode | null = null
    if (o.auto.filter.kind !== 'off') {
      sweep = this.biquad(o.auto.filter.kind, o.auto.filter.kind === 'lowpass' ? 20000 : 20)
      head.connect(sweep)
      head = sweep
    }
    head.connect(o.master)
    this.nodes.push(src, volume, eqLow, eqMid, eqHigh, ...(sweep ? [sweep] : []))

    const toPts = (curve: CurvePoint[]) =>
      curve.map((p) => ({ t: o.winStart + p.beat * o.beat, v: p.value }))
    schedule(volume.gain, o.auto.volume.length ? toPts(o.auto.volume) : o.defaultVolume, o.t0, o.fromSec)
    schedule(eqLow.gain, toPts(o.auto.eq_low_db), o.t0, o.fromSec, 0)
    schedule(eqMid.gain, toPts(o.auto.eq_mid_db), o.t0, o.fromSec, 0)
    schedule(eqHigh.gain, toPts(o.auto.eq_high_db), o.t0, o.fromSec, 0)
    if (sweep) {
      const def = o.auto.filter.kind === 'lowpass' ? 20000 : 20
      schedule(sweep.frequency, toPts(o.auto.filter.cutoff_hz), o.t0, o.fromSec, def)
    }

    if (o.tail && o.tail.kind !== 'none') {
      const send = this.ctx.createGain()
      head.connect(send)
      let fx: AudioNode
      if (o.tail.kind === 'delay') {
        const delay = this.ctx.createDelay(2.0)
        delay.delayTime.value = Math.min(o.tail.time_beats * o.beat, 2.0)
        const feedback = this.ctx.createGain()
        feedback.gain.value = o.tail.feedback
        delay.connect(feedback)
        feedback.connect(delay)
        send.connect(delay)
        fx = delay
        this.nodes.push(feedback)
      } else {
        const conv = this.ctx.createConvolver()
        conv.buffer = this.reverbImpulse(2.5)
        send.connect(conv)
        fx = conv
      }
      fx.connect(o.master)
      this.nodes.push(send, fx)
      // Open the send over the last 4 beats so only the exit rings out.
      schedule(
        send.gain,
        [
          { t: o.exitSec - 4 * o.beat, v: 0 },
          { t: o.exitSec, v: o.tail.wet },
        ],
        o.t0,
        o.fromSec,
        0,
      )
    }

    const rel = o.bufStartPreview - o.fromSec
    if (rel >= 0) src.start(o.t0 + rel, 0)
    else if (-rel < o.buf.duration) src.start(o.t0, -rel)
    this.sources.push(src)
  }

  private biquad(type: BiquadFilterType, freq: number): BiquadFilterNode {
    const node = this.ctx.createBiquadFilter()
    node.type = type
    node.frequency.value = freq
    return node
  }

  private reverbImpulse(seconds: number): AudioBuffer {
    const len = Math.floor(seconds * this.ctx.sampleRate)
    const buf = this.ctx.createBuffer(2, len, this.ctx.sampleRate)
    for (let ch = 0; ch < 2; ch++) {
      const data = buf.getChannelData(ch)
      for (let i = 0; i < len; i++) {
        data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 2.2)
      }
    }
    return buf
  }

  stop(): void {
    for (const s of this.sources) {
      try {
        s.stop()
      } catch {
        /* not started yet */
      }
    }
    for (const n of this.nodes) n.disconnect()
    this.sources = []
    this.nodes = []
    this.playing = false
  }

  async close(): Promise<void> {
    this.stop()
    await this.ctx.close()
  }
}

/** Piecewise-linear equal-power fade over the window (8 segments ≈ cos/sin). */
function equalPower(winStart: number, windowSec: number, side: 'out' | 'in'): TimeValue[] {
  const pts: TimeValue[] = []
  for (let i = 0; i <= 8; i++) {
    const x = (i / 8) * (Math.PI / 2)
    pts.push({ t: winStart + (windowSec * i) / 8, v: side === 'out' ? Math.cos(x) : Math.sin(x) })
  }
  return pts
}

/** Schedule a piecewise-linear automation, starting playback at fromSec. */
function schedule(param: AudioParam, pts: TimeValue[], t0: number, fromSec: number, empty = 1): void {
  if (pts.length === 0) {
    param.setValueAtTime(empty, t0)
    return
  }
  param.setValueAtTime(valueAt(pts, fromSec), t0)
  for (const p of pts) {
    if (p.t > fromSec) param.linearRampToValueAtTime(p.v, t0 + (p.t - fromSec))
  }
}

function valueAt(pts: TimeValue[], t: number): number {
  if (t <= pts[0].t) return pts[0].v
  for (let i = 1; i < pts.length; i++) {
    if (t <= pts[i].t) {
      const a = pts[i - 1]
      const b = pts[i]
      const f = b.t === a.t ? 1 : (t - a.t) / (b.t - a.t)
      return a.v + (b.v - a.v) * f
    }
  }
  return pts[pts.length - 1].v
}
