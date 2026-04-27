from __future__ import annotations

import os
import platform
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
    node_port: int
    node_upstream: str
    proxy_timeout_seconds: int
    api_key: str
    dashboard_password: str
    log_level: str
    models_cache_ms: int
    default_model: str
    max_tokens: int
    ls_binary_path: str
    ls_port: int
    codeium_api_url: str



def _parse_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _default_ls_binary_path() -> str:
    explicit = os.environ.get('LS_BINARY_PATH')
    if explicit:
        return explicit
    if sys_platform() == 'darwin':
        arch = platform.machine().lower()
        suffix = 'arm' if arch in {'arm64', 'aarch64'} else 'x64'
        return f"{Path.home()}/.windsurf/language_server_macos_{suffix}"
    return '/opt/windsurf/language_server_linux_x64'


def sys_platform() -> str:
    return os.sys.platform



def load_config() -> Config:
    data_dir_env = os.environ.get('DATA_DIR')
    shared_data_dir = ROOT / data_dir_env if data_dir_env else ROOT
    data_dir = shared_data_dir
    hostname = os.environ.get('HOSTNAME', '')
    if os.environ.get('REPLICA_ISOLATE') == '1' and hostname:
        data_dir = shared_data_dir / f'replica-{hostname}'
    shared_data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    node_port = _parse_int('PORT', 3003)
    port = _parse_int('PYTHON_PORT', 3004)
    node_upstream = os.environ.get('PYTHON_NODE_UPSTREAM', f'http://127.0.0.1:{node_port}')
    return Config(
        root=ROOT,
        shared_data_dir=shared_data_dir,
        data_dir=data_dir,
        port=port,
        node_port=node_port,
        node_upstream=node_upstream,
        proxy_timeout_seconds=max(_parse_int('PYTHON_PROXY_TIMEOUT_SECONDS', 300), 1),
        api_key=os.environ.get('API_KEY', ''),
        dashboard_password=os.environ.get('DASHBOARD_PASSWORD', ''),
        log_level=os.environ.get('LOG_LEVEL', 'info'),
        models_cache_ms=max(_parse_int('PYTHON_MODELS_CACHE_MS', DEFAULT_MODELS_CACHE_MS), 0),
        default_model=os.environ.get('DEFAULT_MODEL', 'claude-4.5-sonnet-thinking'),
        max_tokens=_parse_int('MAX_TOKENS', 8192),
        ls_binary_path=_default_ls_binary_path(),
        ls_port=_parse_int('LS_PORT', 42100),
        codeium_api_url=os.environ.get('CODEIUM_API_URL', 'https://server.self-serve.windsurf.com'),
    )
