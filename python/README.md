# Python staged migration sidecar

This directory contains the first Python implementation step for the repository-wide refactor.

## Current scope

- Native Python routes: `/health`, `/v1/models`, `/auth/status`, `/dashboard`, `/dashboard/i18n/*`, `/dashboard/data/*`
- Native phase-2 dashboard reads: `/dashboard/api/auth`, `/dashboard/api/proxy`, `/dashboard/api/accounts`
- Native phase-2 cloud-backed actions: `/dashboard/api/accounts/refresh-credits`, `/dashboard/api/accounts/:id/refresh-credits`, `/dashboard/api/accounts/:id/rate-limit`
- Shared state source: the same `.env` and `accounts.json` used by the Node server
- Shared proxy source: the same `proxy.json` used by the Node server
- Fallback behavior: every unsupported route is proxied to the existing Node server so the Python sidecar can be introduced without breaking current clients

## Run

```bash
cd /path/to/WindsurfAPI
node src/index.js
PYTHON_PORT=3004 python3 python/main.py
```

Optional environment variables:

- `PYTHON_PORT` — sidecar listen port, default `3004`
- `PYTHON_NODE_UPSTREAM` — Node reference server base URL, default `http://127.0.0.1:${PORT:-3003}`
- `PYTHON_MODELS_CACHE_MS` — cache duration for the model catalog exported from the Node reference implementation
- `PYTHON_PROXY_TIMEOUT_SECONDS` — fallback proxy timeout for requests forwarded to Node, default `300`

## Phase 2 notes

- Account and proxy dashboard reads now come from Python directly using the shared `accounts.json` and `proxy.json` files.
- Credit refresh and pre-flight rate-limit checks now call Windsurf's public Connect-RPC cloud endpoints from Python, reusing the configured per-account or global proxy.
- Unsupported write-heavy dashboard paths still proxy to Node so the migration can keep moving without a risky big-bang cutover.

## Why it exists

The goal is staged migration, not a big-bang rewrite. New low-risk routes can move to Python first while high-risk protocol paths keep flowing through the proven Node implementation until parity is complete.
