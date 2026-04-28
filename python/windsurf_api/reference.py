from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_MODELS_CACHE_MS


class ReferenceNodeClient:
    def __init__(self, root: Path, cache_ms: int = DEFAULT_MODELS_CACHE_MS) -> None:
        self._root = root
        self._script_path = self._root / 'python' / 'reference-node.mjs'
        self._cache_ms = cache_ms
        self._lock = threading.Lock()
        self._models_cache: dict[str, Any] | None = None
        self._models_cached_at = 0.0
        self._model_meta_cache: dict[str, Any] | None = None
        self._model_meta_cached_at = 0.0

    def get_models(self) -> dict[str, Any]:
        now = time.monotonic() * 1000
        with self._lock:
            if self._models_cache is not None and now - self._models_cached_at < self._cache_ms:
                return self._models_cache
        parsed = self._run('models')
        with self._lock:
            self._models_cache = parsed
            self._models_cached_at = time.monotonic() * 1000
        return parsed

    def get_model_meta(self) -> dict[str, Any]:
        now = time.monotonic() * 1000
        with self._lock:
            if self._model_meta_cache is not None and now - self._model_meta_cached_at < self._cache_ms:
                return self._model_meta_cache
        parsed = self._run('model-meta')
        with self._lock:
            self._model_meta_cache = parsed
            self._model_meta_cached_at = time.monotonic() * 1000
        return parsed

    def _run(self, command: str) -> dict[str, Any]:
        if not self._script_path.exists():
            raise FileNotFoundError(f'Node bridge script not found at {self._script_path}')
        try:
            result = subprocess.run(
                ['node', str(self._script_path), command],
                cwd=self._root,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.SubprocessError as exc:
            print(f'[python-sidecar] failed to fetch {command} from Node reference: {exc}', file=sys.stderr, flush=True)
            raise
        return json.loads(result.stdout)
