import { after, before, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const repoRoot = join(fileURLToPath(new URL('..', import.meta.url)));
const nodePort = 33103;
const pythonPort = 33104;
const apiKey = 'phase2-api-key';
const dashboardPassword = 'phase2-dashboard';

let dataDir;
let nodeProc;
let pythonProc;

function startProcess(command, args, env) {
  const proc = spawn(command, args, {
    cwd: repoRoot,
    env: { ...process.env, ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stdout.on('data', () => {});
  proc.stderr.on('data', () => {});
  return proc;
}

async function waitForHttp(url, headers = {}) {
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      const response = await fetch(url, { headers });
      if (response.status < 500) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${url}`);
}

async function getJson(baseUrl, path, headers = {}) {
  const response = await fetch(`${baseUrl}${path}`, { headers });
  return {
    status: response.status,
    body: await response.json(),
  };
}

async function sendJson(baseUrl, path, method, body, headers = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });
  return {
    status: response.status,
    body: await response.json(),
  };
}

before(async () => {
  dataDir = mkdtempSync(join(tmpdir(), 'windsurf-api-parity-test-'));
  writeFileSync(join(dataDir, 'accounts.json'), '[]\n');
  writeFileSync(join(dataDir, 'proxy.json'), JSON.stringify({
    global: {
      type: 'http',
      host: 'proxy.example.com',
      port: 8080,
      username: 'demo',
      password: 'secret',
    },
    perAccount: {
      acct_pro: {
        type: 'socks5',
        host: 'socks.example.com',
        port: 1080,
        username: 'acct',
        password: 'hidden',
      },
    },
  }, null, 2));

  nodeProc = startProcess('node', ['--input-type=module', '-e', "import { startServer } from './src/server.js'; startServer();"], {
    PORT: String(nodePort),
    DATA_DIR: dataDir,
    API_KEY: apiKey,
    DASHBOARD_PASSWORD: dashboardPassword,
    LOG_LEVEL: 'error',
  });
  pythonProc = startProcess('python3', ['python/main.py'], {
    PORT: String(nodePort),
    PYTHON_PORT: String(pythonPort),
    DATA_DIR: dataDir,
    API_KEY: apiKey,
    DASHBOARD_PASSWORD: dashboardPassword,
    LOG_LEVEL: 'error',
  });

  await waitForHttp(`http://127.0.0.1:${nodePort}/health`);
  await waitForHttp(`http://127.0.0.1:${pythonPort}/health`);

  const headers = { 'X-Dashboard-Password': dashboardPassword };
  const freeAdd = await sendJson(`http://127.0.0.1:${nodePort}`, '/dashboard/api/accounts', 'POST', {
    api_key: 'free-key-123456789',
    label: 'free@example.com',
  }, headers);
  const proAdd = await sendJson(`http://127.0.0.1:${nodePort}`, '/dashboard/api/accounts', 'POST', {
    api_key: 'pro-key-123456789',
    label: 'pro@example.com',
  }, headers);
  assert.equal(freeAdd.status, 200);
  assert.equal(proAdd.status, 200);

  await sendJson(`http://127.0.0.1:${nodePort}`, `/dashboard/api/accounts/${freeAdd.body.account.id}`, 'PATCH', {
    tier: 'free',
    blockedModels: ['gpt-4o-mini'],
  }, headers);
  await sendJson(`http://127.0.0.1:${nodePort}`, `/dashboard/api/accounts/${proAdd.body.account.id}`, 'PATCH', {
    tier: 'pro',
  }, headers);
});

after(() => {
  for (const proc of [pythonProc, nodeProc]) {
    if (proc && !proc.killed) proc.kill('SIGTERM');
  }
  if (dataDir) rmSync(dataDir, { recursive: true, force: true });
});

describe('python sidecar staged parity', () => {
  it('matches auth and shared proxy dashboard responses', async () => {
    const headers = { 'X-Dashboard-Password': dashboardPassword, Authorization: `Bearer ${apiKey}` };
    const routes = [
      '/auth/status',
      '/dashboard/api/auth',
      '/dashboard/api/proxy',
    ];
    for (const route of routes) {
      const [nodeRes, pythonRes] = await Promise.all([
        getJson(`http://127.0.0.1:${nodePort}`, route, headers),
        getJson(`http://127.0.0.1:${pythonPort}`, route, headers),
      ]);
      assert.deepEqual(pythonRes, nodeRes, route);
    }
  });

  it('matches shared account list output for staged native dashboard reads', async () => {
    const headers = { 'X-Dashboard-Password': dashboardPassword };
    const [nodeRes, pythonRes] = await Promise.all([
      getJson(`http://127.0.0.1:${nodePort}`, '/dashboard/api/accounts', headers),
      getJson(`http://127.0.0.1:${pythonPort}`, '/dashboard/api/accounts', headers),
    ]);
    assert.deepEqual(pythonRes, nodeRes);
  });
});
