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
        self._runtime_config_path = data_dir / 'runtime-config.json'
        self._stats_path = data_dir / 'stats.json'
        self._model_access_path = data_dir / 'model-access.json'

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
        self._save_json(self._accounts_path, accounts, description='shared accounts state')

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
        data = self._load_json_object(self._proxy_path, {'global': None, 'perAccount': {}})
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

    def get_system_prompts(self) -> dict[str, str]:
        defaults = self._runtime_defaults()['systemPrompts']
        runtime_config = self._load_runtime_config()
        prompts = runtime_config.get('systemPrompts')
        if not isinstance(prompts, dict):
            return dict(defaults)
        merged = dict(defaults)
        for key, value in prompts.items():
            if isinstance(value, str):
                merged[key] = value
        return merged

    def set_system_prompts(self, patch: Any) -> dict[str, str]:
        runtime_config = self._load_runtime_config()
        current = runtime_config.get('systemPrompts')
        if not isinstance(current, dict):
            current = {}
        if isinstance(patch, dict):
            for key, value in patch.items():
                if isinstance(value, str):
                    current[key] = value.strip()
        runtime_config['systemPrompts'] = current
        self._save_json(self._runtime_config_path, runtime_config, description='runtime config')
        return self.get_system_prompts()

    def reset_system_prompt(self, key: str | None) -> dict[str, str]:
        runtime_config = self._load_runtime_config()
        current = runtime_config.get('systemPrompts')
        if not isinstance(current, dict):
            current = {}
        if key:
            current.pop(key, None)
        else:
            current = {}
        runtime_config['systemPrompts'] = current
        self._save_json(self._runtime_config_path, runtime_config, description='runtime config')
        return self.get_system_prompts()

    def get_model_access_config(self) -> dict[str, Any]:
        raw = self._load_json_object(self._model_access_path, {'mode': 'all', 'list': []})
        mode = raw.get('mode') if isinstance(raw, dict) else 'all'
        items = raw.get('list') if isinstance(raw, dict) else []
        return {
            'mode': mode if mode in {'all', 'allowlist', 'blocklist'} else 'all',
            'list': list(items) if isinstance(items, list) else [],
        }

    def set_model_access_config(self, mode: Any = None, items: Any = None) -> dict[str, Any]:
        config = self.get_model_access_config()
        if isinstance(mode, str) and mode in {'all', 'allowlist', 'blocklist'}:
            config['mode'] = mode
        if isinstance(items, list):
            config['list'] = list(items)
        self._save_json(self._model_access_path, config, description='model access config')
        return self.get_model_access_config()

    def add_model_access(self, model_id: Any) -> dict[str, Any]:
        config = self.get_model_access_config()
        if isinstance(model_id, str) and model_id not in config['list']:
            config['list'].append(model_id)
            self._save_json(self._model_access_path, config, description='model access config')
        return self.get_model_access_config()

    def remove_model_access(self, model_id: Any) -> dict[str, Any]:
        config = self.get_model_access_config()
        if isinstance(model_id, str):
            config['list'] = [item for item in config['list'] if item != model_id]
            self._save_json(self._model_access_path, config, description='model access config')
        return self.get_model_access_config()

    def get_stats(self) -> dict[str, Any]:
        raw = self._load_json_object(self._stats_path, self._stats_defaults())
        state = dict(self._stats_defaults())
        if isinstance(raw, dict):
            state.update(raw)
        model_counts = {}
        raw_models = state.get('modelCounts')
        if isinstance(raw_models, dict):
            for model_id, stats in raw_models.items():
                if not isinstance(stats, dict):
                    continue
                sorted_recent = sorted(ms for ms in stats.get('recentMs', []) if isinstance(ms, (int, float)))
                requests = int(stats.get('requests', 0) or 0)
                total_ms = int(stats.get('totalMs', 0) or 0)
                model_counts[model_id] = {
                    'requests': requests,
                    'success': int(stats.get('success', 0) or 0),
                    'errors': int(stats.get('errors', 0) or 0),
                    'totalMs': total_ms,
                    'avgMs': round(total_ms / requests) if requests > 0 else 0,
                    'p50Ms': round(self._percentile(sorted_recent, 0.5)),
                    'p95Ms': round(self._percentile(sorted_recent, 0.95)),
                }
        return {
            'startedAt': int(state.get('startedAt', 0) or 0),
            'totalRequests': int(state.get('totalRequests', 0) or 0),
            'successCount': int(state.get('successCount', 0) or 0),
            'errorCount': int(state.get('errorCount', 0) or 0),
            'modelCounts': model_counts,
            'accountCounts': state.get('accountCounts') if isinstance(state.get('accountCounts'), dict) else {},
            'hourlyBuckets': state.get('hourlyBuckets') if isinstance(state.get('hourlyBuckets'), list) else [],
        }

    def reset_stats(self) -> None:
        self._save_json(self._stats_path, self._stats_defaults(), description='stats state')

    def get_tier_access_payload(self, model_meta: dict[str, Any]) -> dict[str, Any]:
        tier_access = model_meta.get('tierAccess', {})
        models = model_meta.get('models', {})
        return {
            'free': list(tier_access.get('free', [])),
            'pro': list(tier_access.get('pro', [])),
            'unknown': list(tier_access.get('unknown', [])),
            'expired': list(tier_access.get('expired', [])),
            'allModels': list(models.keys()),
        }

    def get_dashboard_models(self, model_meta: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for model_id, info in model_meta.get('models', {}).items():
            if not isinstance(info, dict):
                continue
            out.append({
                'id': model_id,
                'name': info.get('name', model_id),
                'provider': info.get('provider'),
                'credit': info.get('credit') if isinstance(info.get('credit'), (int, float)) else None,
            })
        return out

    def set_global_proxy(self, cfg: Any) -> dict[str, Any]:
        proxy_config = self.load_proxy_config()
        proxy_config['global'] = self._normalize_proxy_config(cfg, proxy_config.get('global'))
        self._save_json(self._proxy_path, proxy_config, description='proxy config')
        return self.get_proxy_config_masked()

    def set_account_proxy(self, account_id: str, cfg: Any) -> dict[str, Any]:
        proxy_config = self.load_proxy_config()
        per_account = proxy_config.get('perAccount', {})
        if not isinstance(per_account, dict):
            per_account = {}
        normalized = self._normalize_proxy_config(cfg, per_account.get(account_id))
        if normalized is None:
            per_account.pop(account_id, None)
        else:
            per_account[account_id] = normalized
        proxy_config['perAccount'] = per_account
        self._save_json(self._proxy_path, proxy_config, description='proxy config')
        return self.get_proxy_config_masked()

    def remove_proxy(self, scope: str, account_id: str | None = None) -> dict[str, Any]:
        proxy_config = self.load_proxy_config()
        if scope == 'global':
            proxy_config['global'] = None
        elif scope == 'account' and account_id:
            per_account = proxy_config.get('perAccount', {})
            if isinstance(per_account, dict):
                per_account.pop(account_id, None)
                proxy_config['perAccount'] = per_account
        self._save_json(self._proxy_path, proxy_config, description='proxy config')
        return self.get_proxy_config_masked()

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

    def _load_runtime_config(self) -> dict[str, Any]:
        raw = self._load_json_object(self._runtime_config_path, self._runtime_defaults())
        if not isinstance(raw, dict):
            return self._runtime_defaults()
        return self._deep_merge(self._runtime_defaults(), raw)

    def _mask_proxy(self, proxy: Any) -> Any:
        if not isinstance(proxy, dict):
            return proxy
        masked = dict(proxy)
        password = masked.pop('password', '')
        masked['hasPassword'] = bool(password)
        return masked

    def _iso_ms(self, timestamp_ms: int) -> str:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{timestamp_ms % 1000:03d}Z'

    def _load_json_object(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f'[python-sidecar] failed to read JSON state from {path}: {type(exc).__name__}: {exc}',
                file=sys.stderr,
                flush=True,
            )
            return default

    def _save_json(self, path: Path, payload: Any, *, description: str) -> None:
        temp_path = path.with_suffix(path.suffix + '.tmp')
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp_path, path)
        except OSError as exc:
            print(
                f'[python-sidecar] failed to write {description} to {path}: {type(exc).__name__}: {exc}',
                file=sys.stderr,
                flush=True,
            )
            try:
                temp_path.unlink()
            except OSError:
                pass

    def _runtime_defaults(self) -> dict[str, Any]:
        return {
            'experimental': {
                'cascadeConversationReuse': True,
                'preflightRateLimit': False,
            },
            'systemPrompts': {
                'toolReinforcement': 'The functions listed above are available and callable. When the user\'s request can be answered by calling a function, emit a <tool_call> block as described. Use this exact format: <tool_call>{"name":"...","arguments":{...}}</tool_call>',
                'communicationWithTools': 'You are accessed via API. When asked about your identity, describe your actual underlying model name and provider accurately. STRICTLY respond in the exact same language the user used in their latest message (Chinese → Chinese, English → English, Japanese → Japanese; never switch mid-conversation). Use the functions above when relevant.',
                'communicationNoTools': 'You are accessed via API. When asked about your identity, describe your actual underlying model name and provider accurately. Answer directly. STRICTLY respond in the exact same language the user used in their latest message (Chinese → Chinese, English → English, Japanese → Japanese; never switch mid-conversation).',
            },
        }

    def _stats_defaults(self) -> dict[str, Any]:
        return {
            'startedAt': 0,
            'totalRequests': 0,
            'successCount': 0,
            'errorCount': 0,
            'modelCounts': {},
            'accountCounts': {},
            'hourlyBuckets': [],
        }

    def _deep_merge(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        out = dict(base)
        for key, value in override.items():
            if key in {'__proto__', 'constructor', 'prototype'}:
                continue
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                out[key] = self._deep_merge(base[key], value)
            else:
                out[key] = value
        return out

    def _percentile(self, sorted_values: list[float], quantile: float) -> float:
        if not sorted_values:
            return 0
        index = min(len(sorted_values) - 1, int(len(sorted_values) * quantile))
        return float(sorted_values[index])

    def _normalize_proxy_config(self, cfg: Any, existing: Any) -> dict[str, Any] | None:
        if not isinstance(cfg, dict) or not cfg.get('host'):
            return None
        existing_cfg = existing if isinstance(existing, dict) else {}
        return {
            'type': cfg.get('type') or 'http',
            'host': cfg.get('host'),
            'port': self._coerce_port(cfg.get('port')),
            'username': cfg.get('username') or '',
            'password': self._merge_proxy_password(cfg, existing_cfg),
        }

    def _merge_proxy_password(self, new_cfg: dict[str, Any], old_cfg: dict[str, Any]) -> str:
        if 'password' not in new_cfg:
            return str(old_cfg.get('password') or '')
        return str(new_cfg.get('password') or '')

    def _coerce_port(self, value: Any, default: int = 8080) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
