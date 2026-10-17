const { GoogleGenAI } = require('@google/genai');
const { logger } = require('@librechat/data-schemas');
const { tool } = require('@librechat/agents/langchain/tools');
const {
  configureGoogleApiProxyFetch,
  createGeminiImageTool: createGeminiImageToolCore,
} = require('@librechat/api');
const { getStrategyFunctions } = require('~/server/services/Files/strategies');
const { spendTokens, getFiles, createFile } = require('~/models');

configureGoogleApiProxyFetch();

function createGeminiImageTool(fields = {}) {
  return createGeminiImageToolCore({
    ...fields,
    GoogleGenAI,
    tool,
    logger,
    spendTokens,
    getFiles,
    createFile,
    getStrategyFunctions,
  });
}

module.exports = createGeminiImageTool;
module.exports.createGeminiImageTool = createGeminiImageTool;
