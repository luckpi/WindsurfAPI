from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SharedState:
    def __init__(self, shared_data_dir: Path) -> None:
        self._accounts_path = shared_data_dir / 'accounts.json'

    def load_accounts(self) -> list[dict[str, Any]]:
        if not self._accounts_path.exists():
            return []
        try:
            data = json.loads(self._accounts_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def account_counts(self) -> dict[str, int]:
        accounts = self.load_accounts()
        return {
            'total': len(accounts),
            'active': sum(1 for account in accounts if account.get('status') == 'active'),
            'error': sum(1 for account in accounts if account.get('status') == 'error'),
        }

    def is_authenticated(self) -> bool:
        return self.account_counts()['active'] > 0
