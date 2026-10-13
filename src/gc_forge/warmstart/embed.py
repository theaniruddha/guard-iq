from __future__ import annotations

"""
Pluggable embedders for warm-start clustering.

Backends:
    "local"   — hashed TF vectors, pure Python, offline, zero deps. Default.
    "openai"  — OpenAI embeddings API (needs OPENAI_API_KEY). Better semantic
                grouping when logs are large / phrased very differently.
    (the third route, "llm", skips embeddings entirely — see classify.py)

Vectors are sparse dicts {dim_index: weight}, L2-normalised, so cosine is a
simple dot product and both backends share the clustering code.
"""

import hashlib
import math
import os
import re

import httpx

from gc_forge.warmstart.cache import JsonCache, content_key

Vector = dict[int, float]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_LOCAL_DIM = 8192  # large enough that hash collisions don't create fake similarity
_STOP = frozenset(
    "a an the and or for to of in on with that this is are was were be been i you we my your it its "
    "do does did can could will would should have has had not no yes please hi hello thanks thank".split()
)


def _stem(token: str) -> str:
    """Tiny suffix-stripper so 'points'/'point' and 'catering'/'cater' agree."""
    if token.endswith("ies") and len(token) >= 5:
        token = token[:-3] + "y"  # parties -> party
    else:
        for suffix in ("ing", "es", "ed", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                token = token[: -len(suffix)]
                break
    return token[:6]


def _tokens(text: str) -> list[tuple[str, float]]:
    toks = [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 2]
    # bigrams bind short phrases ("loyalty points") but at half weight so they
    # don't drown the unigram overlap between paraphrases
    return [(t, 1.0) for t in toks] + [(f"{a}_{b}", 0.5) for a, b in zip(toks, toks[1:])]


def _hash_dim(token: str) -> int:
    return int(hashlib.md5(token.encode()).hexdigest()[:8], 16) % _LOCAL_DIM


def _normalise(vec: Vector) -> Vector:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    return {k: v / norm for k, v in vec.items()} if norm else {}


def cosine(a: Vector, b: Vector) -> float:
    if len(b) < len(a):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


class LocalEmbedder:
    """Deterministic hashed-TF embedder. Good enough to group near-duplicate
    asks; no network, no model download."""

    name = "local"
    default_threshold = 0.2

    def embed(self, texts: list[str]) -> list[Vector]:
        out: list[Vector] = []
        for text in texts:
            vec: Vector = {}
            for tok, weight in _tokens(text):
                dim = _hash_dim(tok)
                vec[dim] = vec.get(dim, 0.0) + weight
            out.append(_normalise({k: 1.0 + math.log(v) if v > 1 else v for k, v in vec.items()}))
        return out


class OpenAIEmbedder:
    name = "openai"
    default_threshold = 0.55

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("GC_EMBED_MODEL", "text-embedding-3-small")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not set — use backend='local' or export the key."
            )

    def embed(self, texts: list[str]) -> list[Vector]:
        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=60,
        )
        resp.raise_for_status()
        rows = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [
            _normalise({i: v for i, v in enumerate(row["embedding"]) if v})
            for row in rows
        ]


def get_embedder(backend: str):
    if backend == "local":
        return LocalEmbedder()
    if backend == "openai":
        return OpenAIEmbedder()
    raise ValueError(f"Unknown embedding backend {backend!r} (use 'local' or 'openai')")


def embed_with_cache(embedder, texts: list[str], cache: JsonCache) -> list[Vector]:
    """Embed only texts missing from the cache; unchanged logs cost nothing."""
    model = getattr(embedder, "model", embedder.name)
    keys = [content_key("embed", embedder.name, model, t) for t in texts]
    vectors: list[Vector | None] = []
    missing_idx: list[int] = []
    for i, key in enumerate(keys):
        hit = cache.get(key)
        if hit is None:
            vectors.append(None)
            missing_idx.append(i)
        else:
            vectors.append({int(k): v for k, v in hit.items()})

    if missing_idx:
        fresh = embedder.embed([texts[i] for i in missing_idx])
        for i, vec in zip(missing_idx, fresh):
            vectors[i] = vec
            cache.set(keys[i], {str(k): v for k, v in vec.items()})
    return vectors  # type: ignore[return-value]
