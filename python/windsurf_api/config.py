from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS_CACHE_MS = 10_000


def load_env_file(root: Path = ROOT) -> None:
    env_path = root / '.env'
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {'\"', "'"}:
            value = value[1:-1]
        else:
            comment_idx = value.find(' #')
            if comment_idx != -1:
                value = value[:comment_idx].strip()
        os.environ.setdefault(key, value)


load_env_file()


@dataclass(frozen=True)
class Config:
    root: Path
    shared_data_dir: Path
    data_dir: Path
    port: int
    node_upstream: str
    proxy_timeout_seconds: int
    api_key: str
    dashboard_password: str
    log_level: str
    models_cache_ms: int



def _parse_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default



def load_config() -> Config:
    data_dir_env = os.environ.get('DATA_DIR')
    shared_data_dir = ROOT / data_dir_env if data_dir_env else ROOT
    data_dir = shared_data_dir
    hostname = os.environ.get('HOSTNAME', '')
    if os.environ.get('REPLICA_ISOLATE') == '1' and hostname:
        data_dir = shared_data_dir / f'replica-{hostname}'
    shared_data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    port = _parse_int('PYTHON_PORT', 3004)
    node_port = _parse_int('PORT', 3003)
    node_upstream = os.environ.get('PYTHON_NODE_UPSTREAM', f'http://127.0.0.1:{node_port}')
    return Config(
        root=ROOT,
        shared_data_dir=shared_data_dir,
        data_dir=data_dir,
        port=port,
        node_upstream=node_upstream,
        proxy_timeout_seconds=max(_parse_int('PYTHON_PROXY_TIMEOUT_SECONDS', 300), 1),
        api_key=os.environ.get('API_KEY', ''),
        dashboard_password=os.environ.get('DASHBOARD_PASSWORD', ''),
        log_level=os.environ.get('LOG_LEVEL', 'info'),
        models_cache_ms=max(_parse_int('PYTHON_MODELS_CACHE_MS', DEFAULT_MODELS_CACHE_MS), 0),
    )
