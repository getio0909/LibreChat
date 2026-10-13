const { readFileSync } = require('node:fs');
const { configureCADDeployment } = require('@librechat/api');

process.stdout.write(
  configureCADDeployment(
    readFileSync('/app/librechat.yaml', 'utf8'),
    JSON.parse(readFileSync(0, 'utf8')),
  ),
);
