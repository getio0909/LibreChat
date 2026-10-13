import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { settingsSchema } from './config.ts';
import { imageProviders } from './images.ts';
import { createHandler } from './mcp.ts';

const settings = settingsSchema.parse(JSON.parse(readFileSync('/run/secrets/design.json', 'utf8')));
createServer(createHandler(settings, imageProviders(settings))).listen(8000, '0.0.0.0');
