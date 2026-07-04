"""Pydantic models shared by the API. Mirrored in frontend/src/types.ts."""

from pydantic import BaseModel, Field


class Section(BaseModel):
    """A detected structural region of a track (crude in Phase 1 scaffold)."""

    label: str  # intro | build | drop | break | outro
    start_sec: float
    end_sec: float


class TrackOut(BaseModel):
    id: int
    path: str
    filename: str
    duration_sec: float | None
    analysis_status: str
    analysis_error: str | None = None
    bpm: float | None = None
    key_name: str | None = None
    camelot: str | None = None
    energy: float | None = None


class AnalysisOut(BaseModel):
    track_id: int
    bpm: float
    beat_offset_sec: float
    key_name: str | None
    camelot: str | None
    energy: float | None
    sections: list[Section]


class WaveformOut(BaseModel):
    """Downsampled waveform: one absolute peak (0..1) per bin, plus per-bin
    spectral band levels and full-band RMS (both may be absent in caches
    written by older versions — consumers must recompute or fall back).

    bands[i] = [low, mid, high] RMS of the bin, crossovers ~200 Hz / ~4 kHz —
    low tracks kick+bass, mid melodies/vocals, high hats/air. Feeds the
    spectral waveform display, loudness matching and content-aware auto-EQ.
    """

    track_id: int
    bin_sec: float  # seconds of audio covered by each peak bin
    duration_sec: float
    peaks: list[float]
    bands: list[list[float]] | None = None
    rms: list[float] | None = None


class CurvePoint(BaseModel):
    """One point of an automation curve; values interpolate linearly between
    points and hold flat before the first / after the last point.

    `beat` counts from the start of that side's transition window: the
    outgoing side's window is the `blend_beats` before its exit point, the
    incoming side's window is the `blend_beats` after its entry.
    """

    beat: float
    value: float


class FilterSweep(BaseModel):
    """Low/high-pass sweep — mirrors a single Web Audio BiquadFilterNode."""

    kind: str = "off"  # off | lowpass | highpass
    cutoff_hz: list[CurvePoint] = Field(default_factory=list)  # 20..20000


class TailFX(BaseModel):
    """Reverb/delay tail that lets the outgoing track ring out as it exits."""

    kind: str = "none"  # none | reverb | delay
    wet: float = 0.3  # 0..1
    time_beats: float = 0.75  # delay time in beats (delay only)
    feedback: float = 0.45  # 0..0.9 (delay only)


class SamplePlacement(BaseModel):
    """One built-in sample-pack one-shot placed on the seam's beat grid.

    `beat` uses the same domain as CurvePoint: outgoing-track beats counting
    from the transition window start (negative reaches into the preview
    lead, values past `blend_beats` land after the exit). Beat-synced kinds
    (riser, noise) span `beats` beats synthesized at the outgoing tempo;
    impact and crash are fixed-length one-shots that ignore `beats`.
    """

    kind: str  # riser | noise | impact | crash
    beat: float = 0.0
    beats: float = 16.0
    gain_db: float = -6.0


class SampleKindOut(BaseModel):
    kind: str
    label: str
    beat_synced: bool


class StemMix(BaseModel):
    """Per-stem gains for one seam side, applied across that side's
    transition window (Phase 2). All-unity means passthrough — the original
    master plays and no separated stems are required. Anything else needs
    the track's stems separated first (see /api/library/tracks/{id}/stems).
    """

    drums: float = 1.0
    bass: float = 1.0
    vocals: float = 1.0
    other: float = 1.0

    @property
    def active(self) -> bool:
        return any(getattr(self, n) != 1.0 for n in ("drums", "bass", "vocals", "other"))


class StemsOut(BaseModel):
    """Separation state of one track's stem cache."""

    track_id: int
    status: str  # none | pending | running | done | error
    error: str | None = None


class SideAutomation(BaseModel):
    """Automation lanes for one side of a seam.

    Empty lanes mean "template default": equal-power fade for 'blend', unity
    gain for 'cut', EQ flat, no sweep. The parameter set is deliberately
    limited to what Web Audio can automate live so the client preview can
    mirror the server render (DESIGN.md #8, risk #1).
    """

    volume: list[CurvePoint] = Field(default_factory=list)  # linear gain 0..1
    eq_low_db: list[CurvePoint] = Field(default_factory=list)  # -26..+6 dB
    eq_mid_db: list[CurvePoint] = Field(default_factory=list)
    eq_high_db: list[CurvePoint] = Field(default_factory=list)
    filter: FilterSweep = Field(default_factory=FilterSweep)


class SeamParams(BaseModel):
    """Parameters of one transition.

    Times are in seconds within the respective source track (untouched file).
    Older project files without the automation fields load with defaults.
    """

    template: str = "blend"  # blend | cut
    out_point_sec: float | None = None  # where the outgoing track exits
    in_point_sec: float = 0.0  # where the incoming track enters from
    blend_beats: int = 32  # transition window length (in beats of the outgoing track)
    out_auto: SideAutomation = Field(default_factory=SideAutomation)
    in_auto: SideAutomation = Field(default_factory=SideAutomation)
    tail: TailFX = Field(default_factory=TailFX)
    samples: list[SamplePlacement] = Field(default_factory=list)
    out_stems: StemMix = Field(default_factory=StemMix)
    in_stems: StemMix = Field(default_factory=StemMix)


class Seam(BaseModel):
    out_track_id: int
    in_track_id: int
    params: SeamParams = Field(default_factory=SeamParams)


class Project(BaseModel):
    name: str
    track_ids: list[int] = Field(default_factory=list)
    # seams[i] is the transition between track_ids[i] and track_ids[i+1];
    # kept as a keyed list so reordering tracks can preserve crafted seams.
    seams: list[Seam] = Field(default_factory=list)
    # Per-track level trim in dB, applied to the whole track in preview and
    # render. Seeded by the auto-gain endpoint (loudness matching toward the
    # set median), fully user-editable in the timeline.
    track_gains: dict[int, float] = Field(default_factory=dict)


class TrackGainOut(BaseModel):
    """Suggested set-trim for one track (auto gain-matching)."""

    track_id: int
    loudness_db: float | None  # 95th-pctile bin RMS in dBFS
    gain_db: float  # suggested trim toward the set's median loudness


class AutoEQOut(BaseModel):
    """Content-aware EQ seed: ordinary editable curves + why they look so."""

    out_auto: SideAutomation
    in_auto: SideAutomation
    rationale: str


class AdjacencyScore(BaseModel):
    out_track_id: int
    in_track_id: int
    bpm_gap_pct: float
    camelot_distance: int | None
    score: float  # lower is better


class OrderSuggestion(BaseModel):
    track_ids: list[int]
    adjacencies: list[AdjacencyScore]


class SeamSuggestion(BaseModel):
    params: SeamParams
    rationale: str


class SeamPreviewOut(BaseModel):
    """Hybrid-preview metadata; the client fetches the two WAV segments and
    applies all volume/EQ/filter/tail automation itself via Web Audio."""

    key: str
    sample_rate: int
    tau0_sec: float  # preview t=0 expressed in outgoing-track time
    entry_sec: float  # where the incoming segment starts, in preview time
    window_sec: float
    duration_sec: float
    out_url: str
    in_url: str
