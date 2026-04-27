from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class ReferenceNodeClient:
    def __init__(self, root: Path, cache_ms: int = 10000) -> None:
        self._root = root
        self._cache_ms = cache_ms
        self._lock = threading.Lock()
        self._models_cache: dict[str, Any] | None = None
        self._models_cached_at = 0.0

    def get_models(self) -> dict[str, Any]:
        now = time.monotonic() * 1000
        with self._lock:
            if self._models_cache is not None and now - self._models_cached_at < self._cache_ms:
                return self._models_cache
        result = subprocess.run(
            ['node', str(self._root / 'scripts' / 'python-reference.mjs'), 'models'],
            cwd=self._root,
            check=True,
            capture_output=True,
            text=True,
        )
        parsed = json.loads(result.stdout)
        with self._lock:
            self._models_cache = parsed
            self._models_cached_at = time.monotonic() * 1000
        return parsed
