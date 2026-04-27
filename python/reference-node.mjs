import { handleModels } from '../src/handlers/models.js';

const command = process.argv[2] || '';

if (command === 'models') {
  process.stdout.write(JSON.stringify(handleModels()));
  process.exit(0);
}

process.stderr.write(`Unknown python reference command: ${command}\n`);
process.exit(1);
