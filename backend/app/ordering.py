"""Track order suggestion: greedy nearest-neighbor over BPM/key/energy.

Starts from the lowest-BPM track (hard-dance sets climb in tempo) and always
picks the most compatible unused track next. Presented as a suggestion; the
user can freely reorder (DESIGN.md #10).
"""

from .models import AdjacencyScore, OrderSuggestion

# Weights for the compatibility cost. Tune by ear once real sets exist.
_W_BPM = 1.0  # per percent of BPM gap
_W_KEY = 2.0  # per Camelot step
_W_ENERGY = 3.0  # penalty for big energy drops


def camelot_distance(a: str | None, b: str | None) -> int | None:
    """Steps on the Camelot wheel; 0 = same, 1 = mixable neighbor."""
    if not a or not b:
        return None
    na, la = int(a[:-1]), a[-1]
    nb, lb = int(b[:-1]), b[-1]
    ring = min((na - nb) % 12, (nb - na) % 12)
    return ring + (0 if la == lb else 1)


def _cost(a: dict, b: dict) -> tuple[float, float, int | None]:
    bpm_a, bpm_b = a.get("bpm") or 150.0, b.get("bpm") or 150.0
    bpm_gap_pct = abs(bpm_a - bpm_b) / bpm_a * 100
    key_dist = camelot_distance(a.get("camelot"), b.get("camelot"))
    energy_drop = max(0.0, (a.get("energy") or 0.5) - (b.get("energy") or 0.5))
    cost = _W_BPM * bpm_gap_pct + _W_KEY * (key_dist if key_dist is not None else 2) + _W_ENERGY * energy_drop
    return cost, bpm_gap_pct, key_dist


def suggest_order(tracks: list[dict]) -> OrderSuggestion:
    """tracks: dicts with id, bpm, camelot, energy (analyzed tracks only)."""
    if not tracks:
        return OrderSuggestion(track_ids=[], adjacencies=[])

    remaining = sorted(tracks, key=lambda t: t.get("bpm") or 150.0)
    order = [remaining.pop(0)]
    adjacencies: list[AdjacencyScore] = []
    while remaining:
        current = order[-1]
        best = min(remaining, key=lambda t: _cost(current, t)[0])
        remaining.remove(best)
        cost, bpm_gap_pct, key_dist = _cost(current, best)
        adjacencies.append(
            AdjacencyScore(
                out_track_id=current["id"],
                in_track_id=best["id"],
                bpm_gap_pct=round(bpm_gap_pct, 2),
                camelot_distance=key_dist,
                score=round(cost, 2),
            )
        )
        order.append(best)
    return OrderSuggestion(track_ids=[t["id"] for t in order], adjacencies=adjacencies)
