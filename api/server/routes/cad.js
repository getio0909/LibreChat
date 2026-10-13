const { Router } = require('express');
const files = require('node:fs/promises');
const { logger } = require('@librechat/data-schemas');
const { createCADController } = require('@librechat/api');
const { requireJwtAuth, configMiddleware } = require('~/server/middleware');

const controller = createCADController({ files, reportError: logger.error.bind(logger) });
const router = Router();
router.use(requireJwtAuth, configMiddleware);
router.get('/', controller.list);
router.get('/:filename', controller.download);
module.exports = router;
