from __future__ import annotations

"""
Tiny content-addressed JSON cache so nightly re-runs skip re-embedding and
re-classifying unchanged logs. Keys are sha256 of (backend|model|content);
identical text under the same backend always hits.
"""

import hashlib
import json
from pathlib import Path
from typing import Any


def content_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class JsonCache:
    def __init__(self, path: str | Path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self._data: dict[str, Any] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        if enabled and self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        value = self._data.get(key)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        self._data[key] = value
        self._dirty = True

    def save(self) -> None:
        if not self.enabled or not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data))
        self._dirty = False
