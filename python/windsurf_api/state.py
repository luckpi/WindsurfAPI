from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TIER_RPM = {'pro': 60, 'free': 10, 'unknown': 20, 'expired': 0}
RPM_WINDOW_MS = 60 * 1000
PRO_PLAN_RE = re.compile(r'pro|teams|enterprise|trial|individual|premium|paid', re.I)
FREE_PLAN_RE = re.compile(r'free', re.I)


class SharedState:
    def __init__(self, shared_data_dir: Path, data_dir: Path) -> None:
        self._accounts_path = shared_data_dir / 'accounts.json'
        self._proxy_path = data_dir / 'proxy.json'

    def load_accounts(self) -> list[dict[str, Any]]:
        if not self._accounts_path.exists():
            return []
        try:
            data = json.loads(self._accounts_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f'[python-sidecar] failed to read shared accounts state from {self._accounts_path}: '
                f'{type(exc).__name__}: {exc}',
                file=sys.stderr,
                flush=True,
            )
            return []
        return data if isinstance(data, list) else []

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        temp_path = self._accounts_path.with_suffix('.json.tmp')
        try:
            temp_path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp_path, self._accounts_path)
        except OSError as exc:
            print(
                f'[python-sidecar] failed to write shared accounts state to {self._accounts_path}: '
                f'{type(exc).__name__}: {exc}',
                file=sys.stderr,
                flush=True,
            )
            try:
                temp_path.unlink()
            except OSError:
                pass

    def account_counts(self) -> dict[str, int]:
        accounts = self.load_accounts()
        return {
            'total': len(accounts),
            'active': sum(1 for account in accounts if account.get('status') == 'active'),
            'error': sum(1 for account in accounts if account.get('status') == 'error'),
        }

    def is_authenticated(self) -> bool:
        return self.account_counts()['active'] > 0

    def load_proxy_config(self) -> dict[str, Any]:
        if not self._proxy_path.exists():
            return {'global': None, 'perAccount': {}}
        try:
            data = json.loads(self._proxy_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f'[python-sidecar] failed to read proxy config from {self._proxy_path}: '
                f'{type(exc).__name__}: {exc}',
                file=sys.stderr,
                flush=True,
            )
            return {'global': None, 'perAccount': {}}
        if not isinstance(data, dict):
            return {'global': None, 'perAccount': {}}
        per_account = data.get('perAccount')
        return {
            'global': data.get('global'),
            'perAccount': per_account if isinstance(per_account, dict) else {},
        }

    def get_proxy_config_masked(self) -> dict[str, Any]:
        cfg = self.load_proxy_config()
        return {
            'global': self._mask_proxy(cfg.get('global')),
            'perAccount': {
                account_id: self._mask_proxy(proxy)
                for account_id, proxy in cfg.get('perAccount', {}).items()
            },
        }

    def get_effective_proxy(self, account_id: str | None) -> dict[str, Any] | None:
        cfg = self.load_proxy_config()
        if account_id:
            account_proxy = cfg.get('perAccount', {}).get(account_id)
            if isinstance(account_proxy, dict) and account_proxy.get('host'):
                return account_proxy
        global_proxy = cfg.get('global')
        if isinstance(global_proxy, dict) and global_proxy.get('host'):
            return global_proxy
        return None

    def get_account_list(self, model_meta: dict[str, Any]) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        meta_models = model_meta.get('models', {})
        tier_access = model_meta.get('tierAccess', {})
        out: list[dict[str, Any]] = []
        for raw_account in self.load_accounts():
            account = self._with_defaults(raw_account)
            rpm_history = [
                token for token in account.get('_rpmHistory', [])
                if isinstance(token, (int, float)) and token > now - RPM_WINDOW_MS
            ]
            tier_models = list(tier_access.get(account['tier'] or 'unknown') or tier_access.get('unknown') or [])
            out.append({
                'id': account['id'],
                'email': account['email'],
                'method': account['method'],
                'status': account['status'],
                'errorCount': account['errorCount'],
                'lastUsed': self._iso_ms(account['lastUsed']) if account['lastUsed'] else None,
                'addedAt': self._iso_ms(account['addedAt']),
                'keyPrefix': f"{account['apiKey'][:8]}..." if account['apiKey'] else '...',
                'apiKey': account['apiKey'],
                'tier': account['tier'],
                'capabilities': account['capabilities'],
                'lastProbed': account['lastProbed'],
                'rateLimitedUntil': account['rateLimitedUntil'],
                'rateLimited': bool(account['rateLimitedUntil'] and account['rateLimitedUntil'] > now),
                'modelRateLimits': {
                    key: value
                    for key, value in account.get('_modelRateLimits', {}).items()
                    if isinstance(value, (int, float)) and value > now
                },
                'rpmUsed': len(rpm_history),
                'rpmLimit': TIER_RPM.get(account['tier'] or 'unknown', 20),
                'credits': account['credits'],
                'blockedModels': list(account['blockedModels']),
                'availableModels': self._available_models(account, meta_models, tier_models),
                'tierModels': tier_models,
                'userStatus': account['userStatus'],
                'userStatusLastFetched': account['userStatusLastFetched'],
            })
        return out

    def get_account_by_id(self, account_id: str) -> dict[str, Any] | None:
        for account in self.load_accounts():
            if str(account.get('id')) == account_id:
                return self._with_defaults(account)
        return None

    def refresh_credits(self, account_id: str, cloud_client: Any) -> dict[str, Any]:
        accounts = self.load_accounts()
        for account in accounts:
            if str(account.get('id')) != account_id:
                continue
            normalized = self._with_defaults(account)
            try:
                proxy = self.get_effective_proxy(account_id)
                status = cloud_client.get_user_status(normalized['apiKey'], proxy)
                raw = status.pop('raw', None)
                account['credits'] = status
                plan_name = str(status.get('planName') or '')
                if PRO_PLAN_RE.search(plan_name):
                    account['tier'] = 'pro'
                elif FREE_PLAN_RE.search(plan_name) and (account.get('tier') or 'unknown') == 'unknown':
                    account['tier'] = 'free'
                self.save_accounts(accounts)
                return {'ok': True, 'credits': status, 'raw': raw}
            except Exception as exc:
                message = str(exc)
                credits = account.get('credits')
                if isinstance(credits, dict):
                    credits['lastError'] = message
                else:
                    account['credits'] = {'lastError': message, 'fetchedAt': int(time.time() * 1000)}
                self.save_accounts(accounts)
                return {'ok': False, 'error': message}
        return {'ok': False, 'error': 'Account not found'}

    def refresh_all_credits(self, cloud_client: Any) -> list[dict[str, Any]]:
        results = []
        for account in self.load_accounts():
            if account.get('status') != 'active':
                continue
            result = self.refresh_credits(str(account.get('id', '')), cloud_client)
            results.append({
                'id': account.get('id'),
                'email': account.get('email'),
                'ok': result.get('ok', False),
                'error': result.get('error'),
            })
        return results

    def _available_models(
        self,
        account: dict[str, Any],
        meta_models: dict[str, Any],
        tier_models: list[str],
    ) -> list[str]:
        blocked = set(account.get('blockedModels') or [])
        capabilities = account.get('capabilities')
        if account.get('tierManual') or not account.get('userStatusLastFetched') or not isinstance(capabilities, dict):
            return [model for model in tier_models if model not in blocked]
        allowed = []
        tier_model_set = set(tier_models)
        for key, info in meta_models.items():
            if key in blocked:
                continue
            enum_value = info.get('enumValue', 0) if isinstance(info, dict) else 0
            if isinstance(enum_value, int) and enum_value > 0:
                cap = capabilities.get(key) if isinstance(capabilities, dict) else None
                if isinstance(cap, dict) and cap.get('reason') == 'user_status' and cap.get('ok') is True:
                    allowed.append(key)
            elif key in tier_model_set:
                allowed.append(key)
        return allowed

    def _with_defaults(self, account: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': str(account.get('id', '')),
            'email': account.get('email') or '',
            'apiKey': account.get('apiKey') or '',
            'method': account.get('method') or 'api_key',
            'status': account.get('status') or 'active',
            'errorCount': int(account.get('errorCount', 0) or 0),
            'lastUsed': int(account.get('lastUsed', 0) or 0),
            'addedAt': int(account.get('addedAt', 0) or 0),
            'tier': account.get('tier') or 'unknown',
            'tierManual': bool(account.get('tierManual')),
            'capabilities': account.get('capabilities') if isinstance(account.get('capabilities'), dict) else {},
            'lastProbed': int(account.get('lastProbed', 0) or 0),
            'rateLimitedUntil': int(account.get('rateLimitedUntil', 0) or 0),
            '_modelRateLimits': account.get('_modelRateLimits') if isinstance(account.get('_modelRateLimits'), dict) else {},
            '_rpmHistory': account.get('_rpmHistory') if isinstance(account.get('_rpmHistory'), list) else [],
            'credits': account.get('credits'),
            'blockedModels': account.get('blockedModels') if isinstance(account.get('blockedModels'), list) else [],
            'userStatus': account.get('userStatus'),
            'userStatusLastFetched': int(account.get('userStatusLastFetched', 0) or 0),
        }

    def _mask_proxy(self, proxy: Any) -> Any:
        if not isinstance(proxy, dict):
            return proxy
        masked = dict(proxy)
        password = masked.pop('password', '')
        masked['hasPassword'] = bool(password)
        return masked

    def _iso_ms(self, timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
