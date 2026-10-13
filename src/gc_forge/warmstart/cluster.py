from __future__ import annotations

"""
Greedy centroid clustering over the problem-event vectors.

Deliberately simple: events arrive, each joins the closest existing cluster
if similarity >= threshold, else starts a new one. Clusters below
min_size are dropped — warm-start only cares about *recurring* problems,
a one-off failure is noise.
"""

from dataclasses import dataclass, field

from gc_forge.warmstart.embed import Vector, _normalise, cosine
from gc_forge.warmstart.logs import ProblemEvent


@dataclass
class Cluster:
    indices: list[int] = field(default_factory=list)
    centroid: Vector = field(default_factory=dict)

    def events(self, all_events: list[ProblemEvent]) -> list[ProblemEvent]:
        return [all_events[i] for i in self.indices]


def _add_to_centroid(centroid: Vector, vec: Vector, n_before: int) -> Vector:
    merged: Vector = dict(centroid)
    for k, v in vec.items():
        merged[k] = (merged.get(k, 0.0) * n_before + v) / (n_before + 1)
    for k in list(merged):
        if k not in vec:
            merged[k] = merged[k] * n_before / (n_before + 1)
    return _normalise(merged)


def cluster_events(
    vectors: list[Vector],
    threshold: float,
    min_size: int = 2,
) -> list[Cluster]:
    clusters: list[Cluster] = []
    for i, vec in enumerate(vectors):
        if not vec:
            continue
        best: Cluster | None = None
        best_sim = threshold
        for c in clusters:
            sim = cosine(c.centroid, vec)
            if sim >= best_sim:
                best, best_sim = c, sim
        if best is None:
            clusters.append(Cluster(indices=[i], centroid=dict(vec)))
        else:
            best.centroid = _add_to_centroid(best.centroid, vec, len(best.indices))
            best.indices.append(i)

    kept = [c for c in clusters if len(c.indices) >= min_size]
    kept.sort(key=lambda c: -len(c.indices))
    return kept
