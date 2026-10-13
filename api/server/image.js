const { createServer } = require('node:http');
const { createImageMCPHandler } = require('@librechat/api/image');

createServer(createImageMCPHandler({ request: fetch, reportError: console.error })).listen(
  8000,
  '0.0.0.0',
);
