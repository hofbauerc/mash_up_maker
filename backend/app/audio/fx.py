"""Automation engine for the server-side render path.

Mirrors the client preview graph (previewEngine.ts) node for node so the two
DSP paths stay alignable (DESIGN.md #8, risk #1):

    volume envelope -> low shelf 200 Hz -> peak 1.2 kHz -> high shelf 6 kHz
                    -> optional low/high-pass sweep
    outgoing also:  post-chain send (opened over the last 4 beats before the
                    exit) -> feedback delay or convolution reverb tail

Biquad coefficients follow the Web Audio spec formulas (RBJ cookbook, shelf
slope S=1, low/high-pass Q interpreted in dB) — the same math the browser
runs. Filters process in short chunks with parameters re-evaluated from the
seam's automation curves at each chunk, which is how Web Audio's a-rate
AudioParams behave at render-quantum granularity.

Curve semantics match models.CurvePoint: linear interpolation between points,
flat hold before the first and after the last point.
"""

import numpy as np

from ..models import CurvePoint, SideAutomation, TailFX

CHUNK = 512  # samples per parameter update (~12 ms at 44.1k)

# Tail-FX constants shared with previewEngine.ts.
TAIL_SEND_BEATS = 4.0  # the send opens over the last 4 beats before the exit
REVERB_SECONDS = 2.5
_DELAY_MAX_SEC = 2.0
_DELAY_FLOOR = 10 ** (-50 / 20)  # stop feedback taps below -50 dB
_DELAY_RING_CAP_SEC = 8.0

_reverb_ir_cache: dict[int, np.ndarray] = {}


def curve_values(points: list[CurvePoint], beats: np.ndarray) -> np.ndarray | None:
    """Evaluate an automation lane at `beats`; None when the lane is empty."""
    if not points:
        return None
    pts = sorted(points, key=lambda p: p.beat)
    return np.interp(beats, [p.beat for p in pts], [p.value for p in pts])


class _Biquad:
    """One Web-Audio-style biquad with state carried across chunks."""

    def __init__(self, kind: str, sr: int, channels: int):
        self.kind = kind
        self.sr = sr
        self.zi = np.zeros((2, channels))
        self._params: tuple[float, float] | None = None
        self._ba: tuple[np.ndarray, np.ndarray] | None = None

    def process(self, chunk: np.ndarray, freq: float, gain_db: float = 0.0) -> np.ndarray:
        from scipy.signal import lfilter

        if self._params != (freq, gain_db):
            self._params = (freq, gain_db)
            self._ba = _coefficients(self.kind, freq, gain_db, self.sr)
        b, a = self._ba
        out, self.zi = lfilter(b, a, chunk, axis=0, zi=self.zi)
        return out


def _coefficients(kind: str, freq: float, gain_db: float, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Web Audio spec biquad coefficients (Q defaults: 1 linear / 1 dB)."""
    w0 = 2 * np.pi * np.clip(freq, 10.0, sr / 2 * 0.999) / sr
    cw, sw = np.cos(w0), np.sin(w0)
    a_gain = 10 ** (gain_db / 40)

    if kind in ("lowpass", "highpass"):
        alpha = sw / 2 * 10 ** (-1 / 20)  # Q = 1, interpreted in dB
        if kind == "lowpass":
            b = np.array([(1 - cw) / 2, 1 - cw, (1 - cw) / 2])
        else:
            b = np.array([(1 + cw) / 2, -(1 + cw), (1 + cw) / 2])
        a = np.array([1 + alpha, -2 * cw, 1 - alpha])
    elif kind == "peaking":
        alpha = sw / 2  # Q = 1
        b = np.array([1 + alpha * a_gain, -2 * cw, 1 - alpha * a_gain])
        a = np.array([1 + alpha / a_gain, -2 * cw, 1 - alpha / a_gain])
    elif kind in ("lowshelf", "highshelf"):
        k = sw * np.sqrt(2 * a_gain)  # 2*sqrt(A)*alpha with shelf slope S=1
        ap1, am1 = a_gain + 1, a_gain - 1
        if kind == "lowshelf":
            b = a_gain * np.array([ap1 - am1 * cw + k, 2 * (am1 - ap1 * cw), ap1 - am1 * cw - k])
            a = np.array([ap1 + am1 * cw + k, -2 * (am1 + ap1 * cw), ap1 + am1 * cw - k])
        else:
            b = a_gain * np.array([ap1 + am1 * cw + k, -2 * (am1 + ap1 * cw), ap1 + am1 * cw - k])
            a = np.array([ap1 - am1 * cw + k, 2 * (am1 - ap1 * cw), ap1 - am1 * cw - k])
    else:
        raise ValueError(f"unknown biquad kind: {kind}")
    return b / a[0], a / a[0]


def process_side(
    region: np.ndarray,
    sr: int,
    auto: SideAutomation,
    beat_sec: float,
    win_start_sec: float,
    volume_env: np.ndarray | None,
) -> np.ndarray:
    """Run one seam side's automation over `region` ((n, 2), float32).

    `win_start_sec` places curve beat 0 relative to the region start;
    `volume_env` is the per-sample gain (curve or template default), or None
    for unity. EQ lanes hold flat outside their points, exactly like the
    preview's AudioParam ramps.
    """
    out = region.astype(np.float32, copy=True)
    if volume_env is not None:
        out *= volume_env[:, np.newaxis].astype(np.float32)

    eq_lanes = [
        ("lowshelf", 200.0, auto.eq_low_db),
        ("peaking", 1200.0, auto.eq_mid_db),
        ("highshelf", 6000.0, auto.eq_high_db),
    ]
    active_eq = [(kind, freq, pts) for kind, freq, pts in eq_lanes if pts]
    sweep_on = auto.filter.kind in ("lowpass", "highpass")
    if not active_eq and not sweep_on:
        return out

    filters = [_Biquad(kind, sr, out.shape[1]) for kind, _, _ in active_eq]
    sweep = _Biquad(auto.filter.kind, sr, out.shape[1]) if sweep_on else None
    sweep_default = 20000.0 if auto.filter.kind == "lowpass" else 20.0

    for start in range(0, len(out), CHUNK):
        end = min(start + CHUNK, len(out))
        beat = ((start + end) / 2 / sr - win_start_sec) / beat_sec
        chunk = out[start:end].astype(np.float64)
        for f, (_, freq, pts) in zip(filters, active_eq):
            gain = curve_values(pts, np.array([beat]))[0]
            chunk = f.process(chunk, freq, gain)
        if sweep is not None:
            cutoff = curve_values(auto.filter.cutoff_hz, np.array([beat]))
            chunk = sweep.process(chunk, cutoff[0] if cutoff is not None else sweep_default)
        out[start:end] = chunk.astype(np.float32)
    return out


def render_tail(region: np.ndarray, sr: int, tail: TailFX, beat_sec: float) -> np.ndarray | None:
    """Wet tail-FX signal for an outgoing side that ends at the exit.

    `region` is the post-chain audio; the send envelope opens 0 -> wet over
    its last TAIL_SEND_BEATS. Returns wet audio aligned to the region start,
    longer than the region so the tail rings out under the incoming track.
    """
    if tail.kind not in ("delay", "reverb") or tail.wet <= 0 or len(region) == 0:
        return None
    env = np.zeros(len(region), dtype=np.float32)
    ramp = min(len(region), max(1, int(round(TAIL_SEND_BEATS * beat_sec * sr))))
    env[-ramp:] = np.linspace(0.0, tail.wet, ramp, dtype=np.float32)
    send = region * env[:, np.newaxis]

    if tail.kind == "delay":
        return _delay_tail(send, sr, tail, beat_sec)
    return _reverb_tail(send, sr)


def _delay_tail(send: np.ndarray, sr: int, tail: TailFX, beat_sec: float) -> np.ndarray:
    """y[n] = x[n-D] + fb * y[n-D] — a Web Audio DelayNode with feedback."""
    d = max(1, int(round(min(tail.time_beats * beat_sec, _DELAY_MAX_SEC) * sr)))
    fb = float(np.clip(tail.feedback, 0.0, 0.95))
    total = len(send) + d + int(_DELAY_RING_CAP_SEC * sr)
    wet = np.zeros((total, send.shape[1]), dtype=np.float32)
    gain, offset = 1.0, d
    while offset < total and gain > _DELAY_FLOOR:
        end = min(offset + len(send), total)
        wet[offset:end] += gain * send[: end - offset]
        gain *= fb
        offset += d
        if fb == 0.0:
            break
    last = np.nonzero(np.abs(wet).max(axis=1) > 1e-5)[0]
    return wet[: last[-1] + 1] if last.size else wet[:1]


def _reverb_tail(send: np.ndarray, sr: int) -> np.ndarray:
    from scipy.signal import fftconvolve

    ir = _reverb_ir(sr)
    n = len(send) + len(ir) - 1
    wet = np.zeros((n, send.shape[1]), dtype=np.float32)
    for ch in range(send.shape[1]):
        wet[:, ch] = fftconvolve(send[:, ch], ir[:, ch % ir.shape[1]]).astype(np.float32)
    return wet


def _reverb_ir(sr: int) -> np.ndarray:
    """Deterministic exponential-ish noise impulse, energy-normalized per
    channel — the same shape previewEngine.ts synthesizes for its convolver."""
    if sr not in _reverb_ir_cache:
        n = int(REVERB_SECONDS * sr)
        rng = np.random.default_rng(0x5EED)
        ir = rng.uniform(-1.0, 1.0, size=(n, 2)) * (1.0 - np.arange(n) / n)[:, np.newaxis] ** 2.2
        ir /= np.sqrt((ir**2).sum(axis=0, keepdims=True))
        _reverb_ir_cache[sr] = ir.astype(np.float32)
    return _reverb_ir_cache[sr]
