const { createServer } = require('node:http');
const { readFileSync } = require('node:fs');
const { createImageMCPHandler } = require('@librechat/api/image');

createServer(
  createImageMCPHandler({
    request: fetch,
    reportError: console.error,
    configSource: readFileSync('/app/librechat.yaml', 'utf8'),
  }),
).listen(8000, '0.0.0.0');
