from __future__ import annotations

import base64
import http.client
import json
import socket
import ssl
import sys
from typing import Any


SERVER_HOSTS = (
    'server.codeium.com',
    'server.self-serve.windsurf.com',
)
USER_STATUS_PATH = '/exa.seat_management_pb.SeatManagementService/GetUserStatus'
MODEL_CONFIGS_PATH = '/exa.api_server_pb.ApiServerService/GetCascadeModelConfigs'
RATE_LIMIT_PATH = '/exa.api_server_pb.ApiServerService/CheckUserMessageRateLimit'


class ProxyConnectionError(OSError):
    pass


class CloudClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self._timeout_seconds = max(timeout_seconds, 1)
        self._ssl_context = ssl.create_default_context()

    def get_user_status(self, api_key: str, proxy: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {'metadata': _build_metadata(api_key)}
        last_error: Exception | None = None
        proxy_modes = [proxy, None] if proxy else [None]
        for active_proxy in proxy_modes:
            for host in SERVER_HOSTS:
                try:
                    status, data, raw = self._post_json(host, USER_STATUS_PATH, body, active_proxy)
                    if status >= 400:
                        last_error = RuntimeError(f'GetUserStatus {host} → {status}: {raw[:160]}')
                        continue
                    return _normalize_user_status(data)
                except Exception as exc:
                    last_error = exc
                    print(f'[python-sidecar] GetUserStatus {host} failed: {exc}', file=sys.stderr, flush=True)
                    if active_proxy and _is_proxy_error(exc):
                        break
        raise last_error or RuntimeError('GetUserStatus: all hosts failed')

    def get_cascade_model_configs(self, api_key: str, proxy: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {'metadata': _build_metadata(api_key)}
        last_error: Exception | None = None
        proxy_modes = [proxy, None] if proxy else [None]
        for active_proxy in proxy_modes:
            for host in SERVER_HOSTS:
                try:
                    status, data, raw = self._post_json(host, MODEL_CONFIGS_PATH, body, active_proxy)
                    if status >= 400:
                        last_error = RuntimeError(f'GetCascadeModelConfigs {host} → {status}: {raw[:160]}')
                        continue
                    return {
                        'configs': data.get('clientModelConfigs', []),
                        'sorts': data.get('clientModelSorts', []),
                        'defaultOverride': data.get('defaultOverrideModelConfig'),
                    }
                except Exception as exc:
                    last_error = exc
                    print(f'[python-sidecar] GetCascadeModelConfigs {host} failed: {exc}', file=sys.stderr, flush=True)
                    if active_proxy and _is_proxy_error(exc):
                        break
        raise last_error or RuntimeError('GetCascadeModelConfigs: all hosts failed')

    def check_message_rate_limit(self, api_key: str, proxy: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {'metadata': _build_metadata(api_key)}
        last_error: Exception | None = None
        proxy_modes = [proxy, None] if proxy else [None]
        for active_proxy in proxy_modes:
            for host in SERVER_HOSTS:
                try:
                    status, data, raw = self._post_json(host, RATE_LIMIT_PATH, body, active_proxy)
                    if status >= 400:
                        last_error = RuntimeError(f'CheckRateLimit {host} → {status}: {raw[:160]}')
                        continue
                    retry_after = data.get('retryAfterMs')
                    return {
                        'hasCapacity': data.get('hasCapacity') is not False,
                        'messagesRemaining': data.get('messagesRemaining', -1),
                        'maxMessages': data.get('maxMessages', -1),
                        'retryAfterMs': retry_after if isinstance(retry_after, (int, float)) else None,
                    }
                except Exception as exc:
                    last_error = exc
                    print(f'[python-sidecar] CheckRateLimit {host} failed: {exc}', file=sys.stderr, flush=True)
                    if active_proxy and _is_proxy_error(exc):
                        break
        if last_error:
            print(f'[python-sidecar] CheckRateLimit failed: {last_error}', file=sys.stderr, flush=True)
        return {
            'hasCapacity': True,
            'messagesRemaining': -1,
            'maxMessages': -1,
            'retryAfterMs': None,
        }

    def _post_json(
        self,
        host: str,
        path: str,
        body: dict[str, Any],
        proxy: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any], str]:
        payload = json.dumps(body).encode('utf-8')
        if proxy and proxy.get('host'):
            sock = self._connect_via_proxy(proxy, host, 443)
        else:
            sock = socket.create_connection((host, 443), timeout=self._timeout_seconds)
            sock.settimeout(self._timeout_seconds)
            sock = self._ssl_context.wrap_socket(sock, server_hostname=host)
        try:
            request = (
                f'POST {path} HTTP/1.1\r\n'
                f'Host: {host}\r\n'
                'User-Agent: windsurf/1.9600.41\r\n'
                'Accept: application/json\r\n'
                'Connect-Protocol-Version: 1\r\n'
                'Content-Type: application/json\r\n'
                f'Content-Length: {len(payload)}\r\n'
                'Connection: close\r\n'
                '\r\n'
            ).encode('utf-8') + payload
            sock.sendall(request)
            response = http.client.HTTPResponse(sock)
            response.begin()
            raw = response.read().decode('utf-8', errors='replace')
            parsed = json.loads(raw) if raw else {}
            return response.status, parsed, raw
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _connect_via_proxy(self, proxy: dict[str, Any], host: str, port: int) -> ssl.SSLSocket:
        proxy_host = str(proxy.get('host') or '')
        proxy_port = int(proxy.get('port') or 8080)
        sock = socket.create_connection((proxy_host, proxy_port), timeout=self._timeout_seconds)
        sock.settimeout(self._timeout_seconds)
        try:
            proxy_type = str(proxy.get('type') or 'http').lower()
            if proxy_type.startswith('socks'):
                self._handshake_socks5(sock, proxy, host, port)
            else:
                self._handshake_http_connect(sock, proxy, host, port)
            return self._ssl_context.wrap_socket(sock, server_hostname=host)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _handshake_http_connect(self, sock: socket.socket, proxy: dict[str, Any], host: str, port: int) -> None:
        headers = [f'CONNECT {host}:{port} HTTP/1.1', f'Host: {host}:{port}']
        username = str(proxy.get('username') or '')
        if username:
            password = str(proxy.get('password') or '')
            token = base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
            headers.append(f'Proxy-Authorization: Basic {token}')
        request = ('\r\n'.join(headers) + '\r\n\r\n').encode('utf-8')
        sock.sendall(request)
        response = http.client.HTTPResponse(sock)
        response.begin()
        response.read()
        if response.status != 200:
            raise ProxyConnectionError(f'Proxy CONNECT failed: {response.status}')

    def _handshake_socks5(self, sock: socket.socket, proxy: dict[str, Any], host: str, port: int) -> None:
        username = str(proxy.get('username') or '')
        password = str(proxy.get('password') or '')
        methods = [0x00]
        if username:
            methods.append(0x02)
        sock.sendall(bytes([0x05, len(methods), *methods]))
        response = self._recv_exact(sock, 2)
        if response[0] != 0x05:
            raise ProxyConnectionError('SOCKS5 handshake failed')
        method = response[1]
        if method == 0xFF:
            raise ProxyConnectionError('SOCKS5 proxy rejected authentication methods')
        if method == 0x02:
            user_bytes = username.encode('utf-8')
            pass_bytes = password.encode('utf-8')
            sock.sendall(bytes([0x01, len(user_bytes)]) + user_bytes + bytes([len(pass_bytes)]) + pass_bytes)
            auth_response = self._recv_exact(sock, 2)
            if auth_response[1] != 0x00:
                raise ProxyConnectionError('SOCKS5 authentication failed')
        host_bytes = host.encode('idna')
        request = bytes([0x05, 0x01, 0x00, 0x03, len(host_bytes)]) + host_bytes + port.to_bytes(2, 'big')
        sock.sendall(request)
        head = self._recv_exact(sock, 4)
        if head[1] != 0x00:
            raise ProxyConnectionError(f'SOCKS5 CONNECT failed: {head[1]}')
        addr_type = head[3]
        if addr_type == 0x01:
            self._recv_exact(sock, 4 + 2)
        elif addr_type == 0x03:
            length = self._recv_exact(sock, 1)[0]
            self._recv_exact(sock, length + 2)
        elif addr_type == 0x04:
            self._recv_exact(sock, 16 + 2)
        else:
            raise ProxyConnectionError('SOCKS5 proxy returned unknown address type')

    def _recv_exact(self, sock: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = sock.recv(size - len(chunks))
            if not chunk:
                raise ProxyConnectionError('Proxy connection closed unexpectedly')
            chunks.extend(chunk)
        return bytes(chunks)


def _build_metadata(api_key: str) -> dict[str, Any]:
    return {
        'apiKey': api_key,
        'ideName': 'windsurf',
        'ideVersion': '1.9600.41',
        'extensionName': 'windsurf',
        'extensionVersion': '1.9600.41',
        'locale': 'en',
    }


def _normalize_user_status(data: dict[str, Any]) -> dict[str, Any]:
    plan_status = data.get('userStatus', {}).get('planStatus', {})
    plan = plan_status.get('planInfo', {})

    def legacy_div(value: Any) -> float | None:
        return value / 100 if isinstance(value, (int, float)) else None

    def as_unix(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    out = {
        'planName': plan.get('planName', 'Unknown'),
        'dailyPercent': plan_status.get('dailyQuotaRemainingPercent') if isinstance(plan_status.get('dailyQuotaRemainingPercent'), (int, float)) else None,
        'weeklyPercent': plan_status.get('weeklyQuotaRemainingPercent') if isinstance(plan_status.get('weeklyQuotaRemainingPercent'), (int, float)) else None,
        'dailyResetAt': as_unix(plan_status.get('dailyQuotaResetAtUnix')),
        'weeklyResetAt': as_unix(plan_status.get('weeklyQuotaResetAtUnix')),
        'overageBalance': plan_status.get('overageBalanceMicros', 0) / 1_000_000 if isinstance(plan_status.get('overageBalanceMicros'), (int, float)) else None,
        'prompt': {
            'limit': legacy_div(plan.get('monthlyPromptCredits')),
            'used': legacy_div(plan_status.get('usedPromptCredits')),
            'remaining': legacy_div(plan_status.get('availablePromptCredits')),
        },
        'flex': {
            'limit': legacy_div(plan.get('monthlyFlexCreditPurchaseAmount')),
            'used': legacy_div(plan_status.get('usedFlexCredits')),
            'remaining': legacy_div(plan_status.get('availableFlexCredits')),
        },
        'planStart': plan_status.get('planStart'),
        'planEnd': plan_status.get('planEnd'),
        'raw': data,
        'fetchedAt': __import__('time').time_ns() // 1_000_000,
    }
    if out['dailyPercent'] is not None:
        out['percent'] = out['dailyPercent']
    elif out['prompt']['limit'] and out['prompt']['remaining'] is not None:
        out['percent'] = (out['prompt']['remaining'] / out['prompt']['limit']) * 100
    else:
        out['percent'] = None
    return out


def _is_proxy_error(exc: Exception) -> bool:
    return isinstance(exc, ProxyConnectionError) or 'Proxy' in str(exc) or 'SOCKS5' in str(exc)
