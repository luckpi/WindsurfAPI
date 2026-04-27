// Python sidecar bridge: reuse the Node reference implementation for routes
// that have not been fully ported yet, keeping behavior aligned during
// staged migration.
import { handleModels } from '../src/handlers/models.js';
import { MODELS, MODEL_TIER_ACCESS } from '../src/models.js';

const command = process.argv[2] || '';

if (command === 'models') {
  process.stdout.write(JSON.stringify(handleModels()));
  process.exit(0);
}

if (command === 'model-meta') {
  process.stdout.write(JSON.stringify({
    models: Object.fromEntries(
      Object.entries(MODELS).map(([key, info]) => [key, { enumValue: info.enumValue || 0 }])
    ),
    tierAccess: {
      pro: MODEL_TIER_ACCESS.pro,
      free: MODEL_TIER_ACCESS.free,
      unknown: MODEL_TIER_ACCESS.unknown,
      expired: MODEL_TIER_ACCESS.expired,
    },
  }));
  process.exit(0);
}

process.stderr.write(`Unknown python reference command: ${command}\n`);
process.exit(1);
