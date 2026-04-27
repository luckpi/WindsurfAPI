from __future__ import annotations

import http.client
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import Config, load_config
from .reference import ReferenceNodeClient
from .state import SharedState


DASHBOARD_COOKIE_RE = re.compile(r'(?:^|;\s*)dashboard_skin=([^;]+)')
LOCALE_FILE_RE = re.compile(r'^[a-zA-Z0-9\-]+\.json$')
HOP_BY_HOP_HEADERS = {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade',
}


@dataclass(frozen=True)
class AppContext:
    config: Config
    state: SharedState
    reference_node: ReferenceNodeClient
    started_at: float
    package_version: str


class WindsurfPythonServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], context: AppContext) -> None:
        super().__init__(server_address, WindsurfRequestHandler)
        self.context = context


class WindsurfRequestHandler(BaseHTTPRequestHandler):
    server: WindsurfPythonServer
    protocol_version = 'HTTP/1.1'

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._write_common_cors_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self) -> None:
        self._handle_request(with_body=False)

    def do_POST(self) -> None:
        self._handle_request(with_body=True)

    def do_PUT(self) -> None:
        self._handle_request(with_body=True)

    def do_PATCH(self) -> None:
        self._handle_request(with_body=True)

    def do_DELETE(self) -> None:
        self._handle_request(with_body=False)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write('[python-sidecar] ' + format % args + '\n')

    def _handle_request(self, *, with_body: bool) -> None:
        path = self._path_only()
        if path == '/favicon.ico':
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.command == 'GET' and self._is_native_dashboard_request(path):
            self._serve_dashboard_asset(path)
            return
        if self.command == 'GET' and path == '/health':
            self._handle_health()
            return
        if self.command == 'GET' and path == '/v1/models':
            self._handle_models()
            return
        if self.command == 'GET' and path == '/auth/status':
            if not self._validate_api_key():
                self._json(401, {'error': {'message': 'Invalid API key', 'type': 'auth_error'}})
                return
            self._json(200, {
                'authenticated': self.server.context.state.is_authenticated(),
                **self.server.context.state.account_counts(),
            })
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', '0') or '0')) if with_body else b''
        self._proxy_request(body)

    def _handle_health(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        if query.get('verbose') == ['1']:
            self._proxy_request(b'')
            return
        ctx = self.server.context
        version_info = self._git_version_info(ctx.config.root)
        payload = {
            'status': 'ok',
            'provider': 'WindsurfAPI bydwgx1337',
            'version': ctx.package_version,
            'commit': version_info['commit'],
            'commitMessage': version_info['commitMessage'],
            'commitDate': version_info['commitDate'],
            'branch': version_info['branch'],
            'uptime': round(time.time() - ctx.started_at),
            'accounts': ctx.state.account_counts(),
        }
        self._json(200, payload)

    def _handle_models(self) -> None:
        try:
            payload = self.server.context.reference_node.get_models()
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            print(f'[python-sidecar] native /v1/models failed, falling back to Node upstream: {exc}', file=sys.stderr, flush=True)
            self._proxy_request(b'')
            return
        self._json(200, payload)

    def _serve_dashboard_asset(self, path: str) -> None:
        dashboard_root = self.server.context.config.root / 'src' / 'dashboard'
        if path in {'/dashboard', '/dashboard/'}:
            file_name = 'index-sketch.html' if self._dashboard_skin() == 'sketch' else 'index.html'
            self._send_file(dashboard_root / file_name, 'text/html; charset=utf-8', extra_headers={
                'Vary': 'Cookie',
                'Cache-Control': 'no-cache',
            })
            return
        if path.startswith('/dashboard/i18n/'):
            file_name = path.split('/dashboard/i18n/', 1)[1]
            if not LOCALE_FILE_RE.match(file_name):
                self._json(400, {'error': 'Invalid locale file'})
                return
            self._send_file(dashboard_root / 'i18n' / file_name, 'application/json; charset=utf-8')
            return
        if path.startswith('/dashboard/data/'):
            file_name = path.split('/dashboard/data/', 1)[1]
            if not LOCALE_FILE_RE.match(file_name):
                self._json(400, {'error': 'Invalid data file'})
                return
            self._send_file(dashboard_root / 'data' / file_name, 'application/json; charset=utf-8')
            return
        self._proxy_request(b'')

    def _proxy_request(self, body: bytes) -> None:
        upstream = urlsplit(self.server.context.config.node_upstream)
        conn_cls = http.client.HTTPSConnection if upstream.scheme == 'https' else http.client.HTTPConnection
        port = upstream.port or (443 if upstream.scheme == 'https' else 80)
        connection = conn_cls(upstream.hostname, port, timeout=self.server.context.config.proxy_timeout_seconds)
        target = self.path
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS}
        headers['Host'] = upstream.netloc
        headers['X-Forwarded-For'] = self.client_address[0]
        headers['X-Forwarded-Proto'] = 'https' if upstream.scheme == 'https' else 'http'
        try:
            connection.request(self.command, target, body=body if body else None, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.end_headers()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except OSError as exc:
            print(f'[python-sidecar] upstream proxy failed for {self.path}: {exc}', file=sys.stderr, flush=True)
            self._json(502, {'error': {'message': 'Python sidecar upstream proxy failed', 'type': 'proxy_error'}})
        finally:
            connection.close()

    def _send_file(self, path: Path, content_type: str, *, extra_headers: dict[str, str] | None = None) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._json(404, {'error': 'File not found'})
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self._write_common_cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _write_common_cors_headers(self) -> None:
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, x-api-key, anthropic-version, X-Dashboard-Password')

    def _dashboard_skin(self) -> str:
        cookie = self.headers.get('Cookie', '')
        match = DASHBOARD_COOKIE_RE.search(cookie)
        return match.group(1) if match else ''

    def _validate_api_key(self) -> bool:
        expected = self.server.context.config.api_key
        if not expected:
            return True
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
        elif auth:
            token = auth
        else:
            token = self.headers.get('x-api-key', '')
        return token == expected

    def _is_native_dashboard_request(self, path: str) -> bool:
        return path in {'/dashboard', '/dashboard/'} or path.startswith('/dashboard/i18n/') or path.startswith('/dashboard/data/')

    def _path_only(self) -> str:
        return self.path.split('?', 1)[0]

    def _git_version_info(self, root: Path) -> dict[str, str]:
        def run_git(args: list[str]) -> str:
            try:
                result = subprocess.run(
                    ['git', *args],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
            except (subprocess.SubprocessError, OSError):
                return ''
            return result.stdout.strip()

        git_dir = root / '.git'
        if not git_dir.exists():
            return {'commit': '', 'commitMessage': '', 'commitDate': '', 'branch': 'unknown'}
        return {
            'commit': run_git(['rev-parse', '--short', 'HEAD']),
            'commitMessage': run_git(['log', '-1', '--pretty=format:%s']),
            'commitDate': run_git(['log', '-1', '--pretty=format:%cI']),
            'branch': run_git(['rev-parse', '--abbrev-ref', 'HEAD']) or 'unknown',
        }


def _load_package_version(root: Path) -> str:
    package_path = root / 'package.json'
    try:
        package = json.loads(package_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return '0.0.0'
    return str(package.get('version', '0.0.0'))


def build_context(config: Config | None = None) -> AppContext:
    cfg = config or load_config()
    return AppContext(
        config=cfg,
        state=SharedState(cfg.shared_data_dir),
        reference_node=ReferenceNodeClient(cfg.root, cache_ms=cfg.models_cache_ms),
        started_at=time.time(),
        package_version=_load_package_version(cfg.root),
    )


def main() -> None:
    context = build_context()
    server = WindsurfPythonServer(('0.0.0.0', context.config.port), context)
    print(f'[python-sidecar] listening on http://0.0.0.0:{context.config.port}', flush=True)
    print(f'[python-sidecar] proxy upstream {context.config.node_upstream}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
