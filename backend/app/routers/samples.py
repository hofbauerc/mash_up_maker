"""Built-in sample pack (Phase 1.5): kind list + synthesized WAVs.

The browser preview fetches these WAVs and schedules them itself via Web
Audio; the render path synthesizes the identical audio in-memory, so both
DSP paths hear the same sample.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..audio import samples
from ..models import SampleKindOut

router = APIRouter(prefix="/api/samples", tags=["samples"])


@router.get("")
def list_kinds() -> list[SampleKindOut]:
    return [
        SampleKindOut(kind=k.kind, label=k.label, beat_synced=k.beat_synced)
        for k in samples.KINDS.values()
    ]


@router.get("/{kind}.wav")
def sample_wav(kind: str, bpm: float = 150.0, beats: float = 16.0) -> FileResponse:
    """Beat-synced kinds are synthesized at (bpm, beats) so they span exact
    outgoing-track beats; impact/crash ignore both parameters."""
    if kind not in samples.KINDS:
        raise HTTPException(status_code=404, detail=f"unknown sample kind: {kind}")
    return FileResponse(samples.ensure_wav(kind, bpm, beats), media_type="audio/wav")
